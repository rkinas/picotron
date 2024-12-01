import os
import re
import json
import torch
import torch.nn as nn
import torch.distributed as dist
from safetensors import safe_open
import contextlib

from picotron.utils import assert_no_meta_tensors
import picotron.process_group_manager as pgm

@contextlib.contextmanager
def init_model_with_dematerialized_weights(include_buffers: bool = False):
    """
    From Accelerate library: https://github.com/huggingface/accelerate/blob/v0.11.0/src/accelerate/big_modeling.py#L254
    Context manager that initializes models with empty weights (no memory allocation).
    
    Args:
        include_buffers (bool): Whether to also skip buffer initialization.
    """
    old_register_parameter = nn.Module.register_parameter
    if include_buffers:
        old_register_buffer = nn.Module.register_buffer

    def register_empty_parameter(module, name, param):
        old_register_parameter(module, name, param)
        if param is not None:
            param_cls = type(module._parameters[name])
            kwargs = module._parameters[name].__dict__
            module._parameters[name] = param_cls(module._parameters[name].to(torch.device("meta")), **kwargs)

    def register_empty_buffer(module, name, buffer):
        old_register_buffer(module, name, buffer)
        if buffer is not None:
            module._buffers[name] = module._buffers[name].to(torch.device("meta"))

    try:
        nn.Module.register_parameter = register_empty_parameter
        if include_buffers:
            nn.Module.register_buffer = register_empty_buffer
        yield
    finally:
        nn.Module.register_parameter = old_register_parameter
        if include_buffers:
            nn.Module.register_buffer = old_register_buffer

def init_model_with_materialized_weights(model, model_config, hf_hub_checkpoint_path):
    #Initialize model with correct tensor shapes but random weights
    initialization_manager = InitializationManager(model, model_config)
    layer_names = initialization_manager.get_layer_names_in_sft_format()
    print(f"Rank {pgm.process_group_manager.pp_rank} responsible for layers: {layer_names}")
    
    state_dict = {}

    def _process_tensor(sft_name, tensor_handle):
        hf_name = initialization_manager.convert_safetensors_to_hf_name(sft_name)
        tensor = tensor_handle.get_tensor(sft_name)
        tensor = initialization_manager.adjust_tensor_size(tensor, hf_name)
        return hf_name, torch.zeros_like(tensor)

    index_path = os.path.join(hf_hub_checkpoint_path, "model.safetensors.index.json")

    if os.path.exists(index_path): # Handle sharded checkpoint
        with open(index_path, 'r') as f:
            index = json.load(f)
        
        for sft_name in layer_names:
            shard_path = os.path.join(hf_hub_checkpoint_path, index['weight_map'][sft_name])
            with safe_open(shard_path, framework="pytorch", device="cpu") as f:
                hf_name, tensor = _process_tensor(sft_name, f)
                state_dict[hf_name] = tensor

    else: # Handle single file checkpoint
        safetensors_path = os.path.join(hf_hub_checkpoint_path, "model.safetensors")
        with safe_open(safetensors_path, framework="pytorch", device="cpu") as f:
            if len(f.keys()) > len(layer_names):
                print(f"Warning: Checkpoint has {len(f.keys())} layers but model only has {len(layer_names)} layers.")
            
            for sft_name in layer_names:
                hf_name, tensor = _process_tensor(sft_name, f)
                state_dict[hf_name] = tensor

    # Synchronize across distributed processes and load weights
    dist.barrier()
    model.load_state_dict(state_dict, strict=True, assign=True)
    dist.barrier()
    
    assert_no_meta_tensors(model)
    initialization_manager.init_model_parameters()
    
    return model

class InitializationManager:
    def __init__(self, model, model_config):
        self.model = model
        self.model_config = model_config

    def init_model_parameters(self):
        self.model.reset_parameters()

    def get_layer_names_in_sft_format(self):
        """Get layer names in safetensors format based on model's layer distribution."""
        decoder_components = [
            "input_layernorm",
            "mlp.down_proj",
            "mlp.gate_proj",
            "mlp.up_proj",
            "post_attention_layernorm",
            "self_attn.k_proj",
            "self_attn.o_proj",
            "self_attn.q_proj",
            "self_attn.v_proj",
        ]
        
        # Generate base layer names
        layer_names = []
        #TODO: what if there is only tensor parallel that is activated ?
        base_names = [f"model.layers.{id}" for id in self.model.layer_distribution]
        for layer in base_names:
            layer_names.extend(f"{layer}.{component}.weight" for component in decoder_components)
        
        # Add special layers based on pipeline stage
        if pgm.process_group_manager.pp_is_first_stage:
            layer_names.insert(0, "model.embed_tokens.weight")
        elif pgm.process_group_manager.pp_is_last_stage:
            layer_names.extend(["model.norm.weight", "lm_head.weight"])
        
        return layer_names

    def adjust_tensor_size(self, tensor, name):
        """Resize tensor based on architecture changes."""
        if 'attention' not in name:
            return tensor
            
        hidden_size = self.model_config.hidden_size
        head_dim = hidden_size // self.model_config.num_attention_heads
        
        if 'q_proj.weight' in name:
            target_dim = self.model_config.num_attention_heads * head_dim
        elif 'k_proj.weight' in name or 'v_proj.weight' in name:
            target_dim = self.model_config.num_key_value_heads * head_dim
        else:
            return tensor
        
        # Adjust tensor size if needed
        if tensor.shape[0] != target_dim:
            if target_dim > tensor.shape[0]:
                pad_tensor = torch.empty(target_dim - tensor.shape[0], tensor.shape[1], 
                                       dtype=tensor.dtype, device=tensor.device)
                tensor = torch.cat([tensor, pad_tensor], dim=0)
            else:
                tensor = tensor[:target_dim, :]
                
        return tensor

    def convert_safetensors_to_hf_name(self, sft_name):
        """Convert safetensors naming convention to HuggingFace naming convention."""
        name_mapping = {
            "model.": "",
            "layers.": "decoder_layers.",
            "embed_tokens": "embedding",
            "self_attn.": "attention.",
            "o_proj": "out_proj",
            "lm_head": "final_proj",
            "input_layernorm": "input_layernorm",
            "post_attention_layernorm": "post_attention_layernorm",
            r'^norm': 'final_norm'
        }
        
        result = sft_name
        for pattern, replacement in name_mapping.items():
            result = re.sub(pattern, replacement, result)
        return result

class CheckpointManager:
    def __init__(self):
        self.tp_rank = pgm.process_group_manager.tp_rank
        self.pp_rank = pgm.process_group_manager.pp_rank
        self.tp_world_size = pgm.process_group_manager.tp_world_size
        self.pp_world_size = pgm.process_group_manager.pp_world_size
        self.cp_dp_world_size = pgm.process_group_manager.cp_dp_world_size
        self.dp_rank = pgm.process_group_manager.dp_rank
        self.cp_rank = pgm.process_group_manager.cp_rank

    def _get_checkpoint_path(self, out_dir):
        ckpt_name = f"weights_tp_rank_world_size={self.tp_rank}_{self.tp_world_size}_pp_rank_world_size={self.pp_rank}_{self.pp_world_size}.pth"
        return os.path.join(out_dir, ckpt_name)

    def save_checkpoint(self, model, optimizer, trained_steps, trained_tokens, out_dir):
        """Save the model/optimizer states/steps to a checkpoint file."""
        path = self._get_checkpoint_path(out_dir)
        
        # Only DP/CP rank 0 will save the model, the weights are the same across all ranks
        if self.dp_rank == 0 and self.cp_rank == 0:
            os.makedirs(out_dir, exist_ok=True)
            raw_model = model.module if self.cp_dp_world_size > 1 else model
            checkpoint = {
                'model': raw_model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'trained_steps': trained_steps,
                'trained_tokens': trained_tokens
            }
            torch.save(checkpoint, path)

    def load_checkpoint(self, model, optimizer, out_dir):
        """Load the model/optimizer states from the latest checkpoint. Assume the topology is the same."""
        path = self._get_checkpoint_path(out_dir)
        
        if not os.path.exists(path):
            raise FileNotFoundError(f"Checkpoint not found at {path}")
            
        checkpoint = torch.load(path)

        # Load model weights
        raw_model = model.module if self.cp_dp_world_size > 1 else model
        raw_model.load_state_dict(checkpoint['model'])
        
        # Load optimizer state
        optimizer.load_state_dict(checkpoint['optimizer'])
        
        return checkpoint['trained_steps'], checkpoint['trained_tokens']

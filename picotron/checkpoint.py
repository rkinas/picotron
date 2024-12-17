import os
import re
import json
import torch
import torch.nn as nn
import torch.distributed as dist
from safetensors import safe_open
import contextlib
import requests
import subprocess

from picotron.utils import assert_no_meta_tensors, print
import picotron.process_group_manager as pgm

from picotron.pipeline_parallel.pipeline_parallel import PipelineParallel

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

def init_model_with_materialized_weights(model, model_config, save_dir):
    #Initialize model with correct tensor shapes but random weights
    initialization_manager = InitializationManager(model, model_config)

    if pgm.process_group_manager.global_rank == 0:
        available_files = initialization_manager.check_hf_model_files_existences(model_config._name_or_path, os.environ.get("HF_TOKEN"))
        if len(available_files) <= 0:
            raise FileNotFoundError("Safetensors files not found. Please check the model name and authentication token.")

        is_downloaded = initialization_manager.download_hf_model_files(model_config._name_or_path, os.environ.get("HF_TOKEN"), save_dir)
        if not is_downloaded:
            raise FileNotFoundError("Failed to download safetensors files. Please check the model name and authentication token.")

    dist.barrier()
    print(f"Rank {pgm.process_group_manager.global_rank} Safetensors files downloaded successfully")

    layer_names = initialization_manager.get_layer_names_in_sft_format()

    print(f"Rank {pgm.process_group_manager.global_rank} responsible for {len(layer_names)} layers")
    
    if len(layer_names) == 0:
        raise Exception("Some ranks has no layers. There are too many ranks and not enough layers to distribute.")

    state_dict = {}

    def _process_tensor(sft_name, tensor_handle):
        hf_name = initialization_manager.convert_safetensors_to_hf_name(sft_name)
        tensor = tensor_handle.get_tensor(sft_name)
        tensor = initialization_manager.adjust_tensor_size(tensor, hf_name)
        return hf_name, torch.zeros_like(tensor)

    index_path = os.path.join(save_dir, "model.safetensors.index.json")

    if os.path.exists(index_path): # Handle sharded checkpoint
        with open(index_path, 'r') as f:
            index = json.load(f)
        
        for sft_name in layer_names:
            shard_path = os.path.join(save_dir, index['weight_map'][sft_name])
            with safe_open(shard_path, framework="pytorch", device="cpu") as f:
                hf_name, tensor = _process_tensor(sft_name, f)
                state_dict[hf_name] = tensor

    else: # Handle single file checkpoint
        safetensors_path = os.path.join(save_dir, "model.safetensors")
        with safe_open(safetensors_path, framework="pytorch", device="cpu") as f:
            if len(f.keys()) > len(layer_names):
                print(f"Warning: Checkpoint has {len(f.keys())} layers but model only has {len(layer_names)} layers.")
            
            for sft_name in layer_names:
                hf_name, tensor = _process_tensor(sft_name, f)
                state_dict[hf_name] = tensor

    # Force creation of lm_head (even if it is tie_embedding)
    if pgm.process_group_manager.pp_is_last_stage or not isinstance(model, PipelineParallel):
        model.final_proj = nn.Linear(model_config.hidden_size, model_config.vocab_size, bias=False)
        state_dict['final_proj.weight'] = torch.zeros(model_config.vocab_size, model_config.hidden_size)

    # Synchronize across distributed processes and load weights
    dist.barrier()
    model.load_state_dict(state_dict, strict=True, assign=True)
    dist.barrier()

    assert_no_meta_tensors(model)
    # Initialize model parameters
    initialization_manager.init_model_parameters()
    dist.barrier()
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
        if isinstance(self.model, PipelineParallel):
            base_names = [f"model.layers.{id}" for id in self.model.layer_distribution]
        else:
            base_names = [f"model.layers.{id}" for id in range(self.model_config.num_hidden_layers)]
        
        for layer in base_names:
            layer_names.extend(f"{layer}.{component}.weight" for component in decoder_components)
        
        # Add special layers based on pipeline stage or non-PP case
        # NOTE: Safetensors may have tied embeddings, but Picotron does not support it. We always create a new lm_head.
        if isinstance(self.model, PipelineParallel):
            if pgm.process_group_manager.pp_is_first_stage:
                layer_names.insert(0, "model.embed_tokens.weight")
            elif pgm.process_group_manager.pp_is_last_stage:
                layer_names.extend(["model.norm.weight"])
        else:
            layer_names.insert(0, "model.embed_tokens.weight")
            layer_names.extend(["model.norm.weight"])

        return layer_names

    def adjust_tensor_size(self, tensor, name):
        """Resize tensor based on architecture changes and tensor parallelism."""
        tp_rank = pgm.process_group_manager.tp_rank
        tp_size = pgm.process_group_manager.tp_world_size
        hidden_size = self.model_config.hidden_size
        
        # Handle embedding and final projection layers
        if 'embedding.weight' in name or 'final_proj.weight' in name:
            vocab_size = self.model_config.vocab_size
            vocab_per_rank = vocab_size // tp_size
            if tensor.shape[0] != vocab_per_rank:
                start_idx = tp_rank * vocab_per_rank
                end_idx = start_idx + vocab_per_rank
                tensor = tensor[start_idx:end_idx, :]
            return tensor

        # Handle attention layers
        if 'attention' in name:
            head_dim = hidden_size // self.model_config.num_attention_heads
            
            if 'q_proj.weight' in name:
                total_heads = self.model_config.num_attention_heads
                heads_per_rank = total_heads // tp_size
                target_dim = heads_per_rank * head_dim
            elif 'k_proj.weight' in name or 'v_proj.weight' in name:
                total_heads = self.model_config.num_key_value_heads
                heads_per_rank = total_heads // tp_size
                target_dim = heads_per_rank * head_dim
            elif 'out_proj.weight' in name:
                # For out_proj, we split along the second dimension
                target_dim = tensor.shape[0]  # First dimension stays the same
                if tensor.shape[1] != hidden_size // tp_size:
                    tensor = tensor[:, (hidden_size // tp_size) * tp_rank:(hidden_size // tp_size) * (tp_rank + 1)]
                return tensor
            else:
                return tensor
                
            if tensor.shape[0] != target_dim:
                if target_dim > tensor.shape[0]:
                    pad_tensor = torch.empty(target_dim - tensor.shape[0], tensor.shape[1], 
                                        dtype=tensor.dtype, device=tensor.device)
                    tensor = torch.cat([tensor, pad_tensor], dim=0)
                else:
                    tensor = tensor[:target_dim, :]

        # Handle MLP layers
        elif 'mlp' in name:
            intermediate_size = self.model_config.intermediate_size
            intermediate_size_per_rank = intermediate_size // tp_size
            
            if 'up_proj.weight' in name or 'gate_proj.weight' in name:
                if tensor.shape[0] != intermediate_size_per_rank:
                    start_idx = tp_rank * intermediate_size_per_rank
                    end_idx = start_idx + intermediate_size_per_rank
                    tensor = tensor[start_idx:end_idx, :]
            elif 'down_proj.weight' in name:
                if tensor.shape[1] != intermediate_size_per_rank:
                    start_idx = tp_rank * intermediate_size_per_rank
                    end_idx = start_idx + intermediate_size_per_rank
                    tensor = tensor[:, start_idx:end_idx]
                    
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

    def check_hf_model_files_existences(self, model_name, hf_token):
        files_to_check = [
            "model.safetensors",
            "model.safetensors.index.json"
        ]
        
        # Prepare headers with authentication token
        headers = {}
        if hf_token:
            headers["Authorization"] = f"Bearer {hf_token}"
        
        found_files = []
        for file in files_to_check:
            url = f'https://huggingface.co/{model_name}/resolve/main/{file}'
            try:
                # Use GET request with stream=True and authentication headers
                response = requests.get(url, stream=True, headers=headers)
                if response.status_code == 200:
                    found_files.append(file)
                    print(f"✅ Found {file}")
                    response.close()
                elif response.status_code == 401:
                    print(f"❌ Authentication required for {file} (Status: {response.status_code})")
                elif response.status_code == 403:
                    print(f"❌ Access denied for {file} (Status: {response.status_code})")
                else:
                    print(f"❌ Not found {file} (Status: {response.status_code})")
            except Exception as e:
                print(f"❌ Error checking {file}: {str(e)}")
        
        return found_files

    def download_hf_model_files(self, model_name, hf_token, save_dir):        
        files_to_download = ["model.safetensors", "model.safetensors.index.json"]
        downloaded_files = []

        for file in files_to_download:
            if os.path.exists(os.path.join(save_dir, file)):
                print(f"✅ {file} already exists")
                downloaded_files.append(file)
                break

            model_cmd = f"huggingface-cli download {model_name} {file} --local-dir {save_dir} --token {hf_token}"
            print(f"Downloading {file}...")
            result = subprocess.run(model_cmd, shell=True, check=False, stdout=None, stderr=subprocess.PIPE)            
            
            if result.returncode == 0:
                print(f"✅ {file} downloaded successfully")
                downloaded_files.append(file)
                
                # Verify files based on their type
                file_path = os.path.join(save_dir, file)
                if file.endswith('.safetensors'):
                    try:
                        with safe_open(file_path, framework="pytorch", device="cpu") as f:
                            keys = list(f.keys())
                            print(f"✅ Safetensors file is valid")
                            print(f"- Number of tensors: {len(keys)}")
                    except Exception as e:
                        print(f"❌ Error validating safetensors file: {str(e)}")
                        continue
                elif file.endswith('.json'):
                    try:
                        with open(file_path, 'r') as f:
                            index_data = json.load(f)
                            print(f"✅ Index JSON file is valid")
                            print(f"- Number of weight shards: {len(index_data.get('weight_map', {}))}")
                    except Exception as e:
                        print(f"❌ Error validating index JSON file: {str(e)}")
                        continue
            else:
                error_message = result.stderr.decode('utf-8', errors='replace')
                if "404 Client Error" in error_message or "Entry Not Found" in error_message:
                    print(f"❌ File {file} not found in repository")
                else:
                    print(f"❌ Download failed: {error_message.strip()}")

        if len(downloaded_files) == 0:
            print("❌ No files were downloaded")
            return False

        print(f"\nSuccessfully downloaded files: {', '.join(downloaded_files)}")
        return True

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

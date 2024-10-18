# Inspired by https://github.com/zhuzilin/ring-flash-attention
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import distributed as dist
from typing import Any, Optional, Tuple
from src.distributed.distributed_primtives import ContextComms
from model import Attention
import src.distributed.process_group_manager as pgm

class ContextParallel(nn.Module):
    def __init__(self, model, config):
        super().__init__(model, config)

        for name, module in model.named_modules():
            if isinstance(module, Attention) and not isinstance(module, RingAttention):
                parent_name, child_name = name.rsplit('.', 1)
                parent_module = model.get_submodule(parent_name)
                setattr(parent_module, child_name, RingAttention(module))
                del module

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model, name)

class RingAttention(nn.Module):
    def __init__(self, original_mha):
        super().__init__()
    
        self.hidden_size = original_mha.hidden_size
        self.num_heads = original_mha.num_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = original_mha.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.is_causal = original_mha.is_causal

        # Copy the weights from the original Attention
        self.q_proj = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.k_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
        self.v_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
        self.o_proj = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        
        self.q_proj.weight.data.copy_(original_mha.q_proj.weight.data)
        self.k_proj.weight.data.copy_(original_mha.k_proj.weight.data)
        self.v_proj.weight.data.copy_(original_mha.v_proj.weight.data)
        self.o_proj.weight.data.copy_(original_mha.o_proj.weight.data)

        self.rotary = original_mha.rotary

    def forward(self, input_ids, position_ids):
        batch_size, seq_len, _ = input_ids.shape
        
        q = self.q_proj(input_ids)
        k = self.k_proj(input_ids)
        v = self.v_proj(input_ids)
        
        q = q.view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        k = k.view(batch_size, seq_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        v = v.view(batch_size, seq_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        
        if self.rotary is not None:
            cos, sin = self.rotary(v, position_ids)
            q, k = self.rotary.apply_rotary_pos_emb(q, k, cos, sin)
        
        k = self._repeat_kv(k, self.num_key_value_groups)
        v = self._repeat_kv(v, self.num_key_value_groups)
        
        sm_scale = 1.0 / (q.size(-1) ** 0.5)
        output = RingAttentionFunc.apply(q, k, v, sm_scale, self.is_causal)
       
        output = output.transpose(1, 2).contiguous().view(batch_size, seq_len, -1)
        output = self.o_proj(output)
        return output
    
    def _repeat_kv(self, x, n_rep):
        batch, num_key_value_heads, seq_len, head_dim = x.shape
        if n_rep == 1:
            return x
        x = x[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, seq_len, head_dim)
        return x.reshape(batch, num_key_value_heads * n_rep, seq_len, head_dim)

class RingAttentionFunc(torch.autograd.Function):

    @staticmethod
    def forward(ctx, q, k, v, sm_scale, is_causal):
        comm = ContextComms("comm")
        #NOTE: Find a better to save these tensors without cloning
        k_og = k.clone()
        v_og = v.clone()
        out, lse = None, None
        next_k, next_v = None, None

        for step in range(comm.world_size):
            if step + 1 != comm.world_size:
                next_k = comm.send_recv(k)
                next_v = comm.send_recv(v)
                comm.commit()

            if not is_causal or step <= comm.rank:
                block_out, block_lse  = ring_attention_forward(
                    q, k, v, sm_scale, is_causal and step == 0
                )
                out, lse = update_out_and_lse(out, lse, block_out, block_lse)
                
            if step + 1 != comm.world_size:
                comm.wait()
                k = next_k
                v = next_v

        out = out.to(q.dtype)
        ctx.save_for_backward(q, k_og, v_og, out, lse.squeeze(-1))
        ctx.sm_scale = sm_scale
        ctx.is_causal = is_causal
        return out

    @staticmethod
    def backward(ctx, dout, *args):

        q, k, v, out, softmax_lse = ctx.saved_tensors
        sm_scale = ctx.sm_scale
        is_causal = ctx.is_causal

        kv_comm = ContextComms("kv_comm")
        d_kv_comm = ContextComms("d_kv_comm")
        dq, dk, dv = None, None, None
        next_dk, next_dv = None, None
        
        block_dq_buffer = torch.empty(q.shape, dtype=q.dtype, device=q.device)
        block_dk_buffer = torch.empty(k.shape, dtype=k.dtype, device=k.device)
        block_dv_buffer = torch.empty(v.shape, dtype=v.dtype, device=v.device)

        next_dk, next_dv = None, None
        next_k, next_v = None, None

        for step in range(kv_comm.world_size):
            if step + 1 != kv_comm.world_size:
                next_k = kv_comm.send_recv(k)
                next_v = kv_comm.send_recv(v)
                kv_comm.commit()

            if step <= kv_comm.rank or not is_causal:
                bwd_causal = is_causal and step == 0

                block_dq_buffer, block_dk_buffer, block_dv_buffer = ring_attention_backward(
                    dout, q, k, v, out, softmax_lse, sm_scale, bwd_causal
                )

                if dq is None:
                    dq = block_dq_buffer.to(torch.float32)
                    dk = block_dk_buffer.to(torch.float32)
                    dv = block_dv_buffer.to(torch.float32)
                else:
                    dq += block_dq_buffer
                    d_kv_comm.wait()
                    dk = block_dk_buffer + next_dk
                    dv = block_dv_buffer + next_dv
            elif step != 0:
                d_kv_comm.wait()
                dk = next_dk
                dv = next_dv

            if step + 1 != kv_comm.world_size:
                kv_comm.wait()
                k = next_k
                v = next_v

            next_dk = d_kv_comm.send_recv(dk)
            next_dv = d_kv_comm.send_recv(dv)
            d_kv_comm.commit()

        d_kv_comm.wait()

        return dq, next_dk, next_dv, None, None

def ring_attention_forward(q, k, v, sm_scale, is_causal):
    batch_size, nheads, seqlen, d = q.shape
    S = torch.matmul(q, k.transpose(-2, -1)) * sm_scale

    if is_causal:
        causal_mask = torch.triu(torch.ones(seqlen, seqlen, device=q.device, dtype=torch.bool), diagonal=1)
        causal_mask = causal_mask.unsqueeze(0).unsqueeze(1).expand(batch_size, nheads, seqlen, seqlen)
        S.masked_fill_(causal_mask, float('-inf'))

    # Online softmax
    S_max = torch.max(S, dim=-1, keepdim=True)[0]
    exp_S = torch.exp(S - S_max)
    exp_sum = torch.sum(exp_S, dim=-1, keepdim=True)
    log_sum_exp = torch.log(exp_sum) + S_max
    P = exp_S / exp_sum
    O = torch.matmul(P, v)
    return O, log_sum_exp.squeeze(-1)

def ring_attention_backward(dO, Q, K, V, O, softmax_lse, sm_scale, is_causal):
    batch_size, nheads, seqlen, d = Q.shape
    
    # Recreate S and P from log_sum_exp
    S = torch.matmul(Q, K.transpose(-2, -1)) * sm_scale
    if is_causal:
        causal_mask = torch.triu(torch.ones(seqlen, seqlen, device=Q.device, dtype=torch.bool), diagonal=1)
        S = S.masked_fill(causal_mask.unsqueeze(0).unsqueeze(1), float('-inf'))

    P = torch.exp(S - softmax_lse.unsqueeze(-1))
    # Step 1: Compute dV
    dV = torch.matmul(P.transpose(-2, -1), dO)
    # Step 2: Compute dP
    dP = torch.matmul(dO, V.transpose(-2, -1))
    # Step 3: Compute D
    D = torch.sum(dO * O, dim=-1, keepdim=True)
    # Step 4: Compute dS
    dS = P * (dP - D)
    # Apply causal mask to dS if is_causal is True
    if is_causal:
        dS = dS.masked_fill(causal_mask.unsqueeze(0).unsqueeze(1), 0)
    # Step 5: Compute dQ
    dQ = torch.matmul(dS, K) * sm_scale
    # Step 6: Compute dK
    dK = torch.matmul(dS.transpose(-2, -1), Q) * sm_scale
    return dQ, dK, dV

def update_out_and_lse(
    out: Optional[torch.Tensor],
    lse: Optional[torch.Tensor],
    block_out: torch.Tensor,
    block_lse: torch.Tensor,
    slice_: Optional[Any] = None
) -> Tuple[torch.Tensor, torch.Tensor]:
    
    def _update(current_out, current_lse):
        # new_lse = lse + torch.log(1 + torch.exp(block_lse - lse))
        # torch.exp(lse - new_lse) * out + torch.exp(block_lse - new_lse) * block_out
        # For additional context and discussion, please refer to:
        # https://github.com/zhuzilin/ring-flash-attention/pull/34#issuecomment-2076126795
        current_out = current_out - F.sigmoid(block_lse - current_lse) * (current_out - block_out)
        current_lse = current_lse - F.logsigmoid(current_lse - block_lse)
        return current_out, current_lse
    
    block_out = block_out.to(torch.float32)
    block_lse = block_lse.unsqueeze(dim=-1)

    if out is None:
        if slice_ is not None:
            raise RuntimeError("first update_out_and_lse should not pass slice_ args")
        return block_out, block_lse

    if slice_ is not None:
        out[slice_], lse[slice_] = _update(out[slice_], lse[slice_])
    else:
        out, lse = _update(out, lse)
        
    return out, lse
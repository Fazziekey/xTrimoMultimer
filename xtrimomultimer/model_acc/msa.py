# Copyright 2022 BioMap (Beijing) Intelligence Technology Limited
# Copyright 2022 HPC-AI Technology Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from xtrimomultimer.model_acc.kernel import LayerNorm

from xtrimomultimer.model_acc.ops import Transition, SelfAttention, GlobalAttention
from xtrimomultimer.model_acc.kernel import bias_dropout_add
from xtrimomultimer.model_acc.distributed import scatter, row_to_col
from xtrimomultimer.model_acc.distributed.comm_async import gather_async


class MSARowAttentionWithPairBias(nn.Module):
    def __init__(self, d_node, d_pair, c=32, n_head=8, p_drop=0.15):
        super(MSARowAttentionWithPairBias, self).__init__()
        self.d_node = d_node
        self.d_pair = d_pair
        self.c = c
        self.n_head = n_head
        self.p_drop = p_drop

        self.layernormM = LayerNorm(d_node)
        self.layernormZ = LayerNorm(d_pair)

        _init_weights = torch.nn.init.normal_(
            torch.zeros([n_head, d_pair]), std=1.0 / math.sqrt(d_pair)
        )
        self.linear_b_weights = nn.parameter.Parameter(
            data=_init_weights, requires_grad=True
        )

        self.attention = SelfAttention(
            qkv_dim=d_node,
            c=c,
            n_head=n_head,
            out_dim=d_node,
            gating=True,
            last_bias_fuse=True,
        )

        self.out_bias = nn.parameter.Parameter(
            data=torch.zeros((d_node,)), requires_grad=True
        )

    def forward(self, M_raw, Z, M_mask):
        ## Input projections
        M = self.layernormM(M_raw)
        Z = self.layernormZ(Z)
        b = F.linear(Z, self.linear_b_weights)
        b, work = gather_async(b, dim=1)
        # b = rearrange(b, 'b q k h -> b h q k')

        # padding_bias = (1e9 * (M_mask - 1.))[:, :, None, None, :]

        M = self.attention(M, M_mask, (b, work))
        dropout_mask = torch.ones_like(M[:, 0:1, :, :], device=M.device, dtype=M.dtype)

        return bias_dropout_add(
            M,
            self.out_bias,
            dropout_mask,
            M_raw,
            prob=self.p_drop,
            training=self.training,
        )


class MSAColumnAttention(nn.Module):
    def __init__(self, d_node, c=32, n_head=8):
        super(MSAColumnAttention, self).__init__()
        self.d_node = d_node
        self.c = c
        self.n_head = n_head

        self.layernormM = LayerNorm(d_node)
        self.attention = SelfAttention(
            qkv_dim=d_node, c=c, n_head=n_head, out_dim=d_node, gating=True
        )

    def forward(self, M_raw, M_mask):
        M = M_raw.transpose(-2, -3)
        M = self.layernormM(M)

        M_mask = M_mask.transpose(-1, -2)
        # padding_bias = (1e9 * (M_mask - 1.))[:, :, None, None, :]

        M = self.attention(M, M_mask)

        M = M.transpose(-2, -3)
        return M_raw + M


class MSAColumnGlobalAttention(nn.Module):
    def __init__(self, d_node, c=8, n_head=8):
        super(MSAColumnGlobalAttention, self).__init__()

        self.d_node = d_node
        self.c = c
        self.n_head = n_head

        self.layernormM = LayerNorm(d_node)
        self.global_attention = GlobalAttention(
            qkv_dim=d_node, c=c, n_head=n_head, out_dim=d_node
        )

    def forward(self, M_raw, M_mask):
        M = M_raw.transpose(-2, -3)
        M = self.layernormM(M)

        M_mask = M_mask.transpose(-1, -2)

        M = self.global_attention(M, M_mask)

        M = M.transpose(-2, -3)
        return M_raw + M


class MSAStack(nn.Module):
    def __init__(self, d_node, d_pair, is_extra=False, p_drop=0.15):
        super(MSAStack, self).__init__()

        self.MSARowAttentionWithPairBias = MSARowAttentionWithPairBias(
            d_node=d_node, d_pair=d_pair, p_drop=p_drop
        )

        self.MSAColumnAttention = MSAColumnAttention(d_node=d_node)
        self.MSATransition = Transition(d=d_node)

    def forward(self, node, pair, node_mask):
        # split node in row-axis
        node_mask_row = scatter(node_mask, dim=1)
        node = self.MSARowAttentionWithPairBias(node, pair, node_mask_row)

        node = row_to_col(node)
        node_mask_col = scatter(node_mask, dim=2)

        node = self.MSAColumnAttention(node, node_mask_col)
        node = self.MSATransition(node)

        return node


class ExtraMSAStack(nn.Module):
    def __init__(self, d_node, d_pair, p_drop=0.15):
        super(ExtraMSAStack, self).__init__()

        self.MSARowAttentionWithPairBias = MSARowAttentionWithPairBias(
            d_node=d_node, d_pair=d_pair, p_drop=p_drop, c=8
        )

        self.MSAColumnAttention = MSAColumnGlobalAttention(d_node=d_node, c=8)
        self.MSATransition = Transition(d=d_node)

    def forward(self, node, pair, node_mask):
        node_mask_row = scatter(node_mask, dim=1)
        node = self.MSARowAttentionWithPairBias(node, pair, node_mask_row)

        node = row_to_col(node)
        node_mask_col = scatter(node_mask, dim=2)

        node = self.MSAColumnAttention(node, node_mask_col)
        node = self.MSATransition(node)

        return node

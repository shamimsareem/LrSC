#MIT License

#Copyright (c) 2025 Hou Sai

#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:

#The above copyright notice and this permission notice shall be included in all
#copies or substantial portions of the Software.

#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#SOFTWARE.
# It contains four different low-rank similarity (bilinear pooling, cross-variance, bilinear outer product and cosine similarity)

import torch
import torch.nn as nn
import torch.nn.functional as F
from lib.config import cfg
import lib.utils_good as utils
import layers

class LowRank(nn.Module):
    def __init__(self, embed_dim, att_type, att_heads, att_mid_dim, att_mid_drop):
        super(LowRank, self).__init__()
        self.embed_dim = embed_dim
        self.num_heads = att_heads
        self.head_dim = embed_dim // self.num_heads
        self.scaling = self.head_dim ** -0.5
        output_dim = 2 * embed_dim if cfg.MODEL.BILINEAR.ACT == 'GLU' else embed_dim

        # Define the dropout layer
        self.dropout = nn.Dropout(p=att_mid_drop)

        sequential = []
        sequential.append(nn.Linear(embed_dim, output_dim))
        act = utils.activation(cfg.MODEL.BILINEAR.ACT)
        if act is not None:
            sequential.append(act)
        sequential.append(torch.nn.GroupNorm(self.num_heads, embed_dim))
        self.in_proj_q = nn.Sequential(*sequential)

        sequential = []
        sequential.append(nn.Linear(embed_dim, output_dim))
        act = utils.activation(cfg.MODEL.BILINEAR.ACT)
        if act is not None:
            sequential.append(act)
        sequential.append(torch.nn.GroupNorm(self.num_heads, embed_dim))
        self.in_proj_k = nn.Sequential(*sequential)

        sequential = []
        sequential.append(nn.Linear(embed_dim, output_dim))
        act = utils.activation(cfg.MODEL.BILINEAR.ACT)
        if act is not None:
            sequential.append(act)
        sequential.append(torch.nn.GroupNorm(self.num_heads, embed_dim))
        self.in_proj_v1 = nn.Sequential(*sequential)

        sequential = []
        sequential.append(nn.Linear(embed_dim, output_dim))
        act = utils.activation(cfg.MODEL.BILINEAR.ACT)
        if act is not None:
            sequential.append(act)
        sequential.append(torch.nn.GroupNorm(self.num_heads, embed_dim))
        self.in_proj_v2 = nn.Sequential(*sequential)

        self.attn_net = layers.create(att_type, att_mid_dim, att_mid_drop) #SCA_att
        self.clear_buffer()

    def apply_to_states(self, fn):
        self.buffer_keys = fn(self.buffer_keys)
        self.buffer_value2 = fn(self.buffer_value2)

    def init_buffer(self, batch_size):
        self.buffer_keys = torch.zeros((batch_size, self.num_heads, 0, self.head_dim)).cuda()
        self.buffer_value2 = torch.zeros((batch_size, self.num_heads, 0, self.head_dim)).cuda()


    def clear_buffer(self):
        self.buffer_keys = None
        self.buffer_value2 = None

    # query -- batch_size * qdim
    # value -- batch_size * att_num * vdim
    def forward(self, query, key, mask, value1, value2, precompute=False):
        batch_size = query.size()[0]
        q = self.in_proj_q(query)
        v1 = self.in_proj_v1(value1)

        q = q.view(batch_size, self.num_heads, self.head_dim)
        v1 = v1.view(batch_size, self.num_heads, self.head_dim)

        if precompute == False:
            key = key.view(-1, key.size()[-1])
            value2 = value2.view(-1, value2.size()[-1])
            k = self.in_proj_k(key)
            v2 = self.in_proj_v2(value2)
            k = k.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
            v2 = v2.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        else:
            k = key
            v2 = value2

        #attn_map = q.unsqueeze(-2) * k
        # Original line:
        # attn_map = q.unsqueeze(-2) * k

        #print(q.shape)
        #print(k.shape)
        #####################
        #Bilinear outer product
        # Normalize q (optional for cosine basis)
        #q_unit = F.normalize(q, p=2, dim=-1)  # (50, 8, 64)

        # Build outer product: (64 x 64) projection operator
        #q_vec = q_unit.unsqueeze(-1)  # (50, 8, 64, 1)
        #qT = q_unit.unsqueeze(-2)  # (50, 8, 1, 64)
        #proj_matrix = torch.matmul(q_vec, qT)  # (50, 8, 64, 64)

        # Apply to each k vector: (50, 8, 144, 64) × (50, 8, 64, 64)
        #score = torch.matmul(k, proj_matrix)  # (50, 8, 144, 64)
        ####################
        # Compute element-wise mean between q and k (assuming k is appropriately unsqueezed or reshaped to match q's dimensions)
        #mean_qk = (q.unsqueeze(-2) + k) / 2

        # Compute cross-variance
        #score = (q.unsqueeze(-2) - mean_qk) ** 2 + (k - mean_qk) ** 2
        #score = ((q.unsqueeze(-2) - mean_qk) ** 2 + (k - mean_qk) ** 2) / 2
        ###################################
        # Compute cosine similarity
        cos_sim = F.cosine_similarity(q.unsqueeze(2), k, dim=-1)  # (50, 8, 144)

        # Reshape for broadcasting
        cos_sim_exp = cos_sim.unsqueeze(-1)  # (50, 8, 144, 1)
        q_dir = F.normalize(q, dim=-1).unsqueeze(2)  # (50, 8, 1, 64)

        # Final projection: each k projected onto direction of q, scaled by cos_sim
        score = cos_sim_exp @ q_dir  # (50, 8, 144, 64)
        #######################################
        #print(cross_variance.shape)
        #exit(0)
        # Apply dropout to cross-variance
        attn_map = self.dropout(score)
        #print(mask.shape)
        #exit(0)

        attn = self.attn_net(attn_map, mask, v1, v2)
        attn = attn.view(batch_size, self.num_heads * self.head_dim)
        return attn

    # query -- batch_size * seq_num * qdim
    # value -- batch_size * att_num * vdim
    def forward2(self, query, key, mask, value1, value2, precompute=False):
        batch_size = query.size()[0]
        query = query.view(-1, query.size()[-1])
        value1 = value1.view(-1, value1.size()[-1])

        q = self.in_proj_q(query)
        v1 = self.in_proj_v1(value1)

        q = q.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        v1 = v1.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)

        if precompute == False:
            key = key.view(-1, key.size()[-1])
            value2 = value2.view(-1, value2.size()[-1])
            k = self.in_proj_k(key)
            v2 = self.in_proj_v2(value2)
            k = k.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
            v2 = v2.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)

            if self.buffer_keys is not None and self.buffer_value2 is not None:
                self.buffer_keys = torch.cat([self.buffer_keys, k], dim=2)
                self.buffer_value2 = torch.cat([self.buffer_value2, v2], dim=2)
                k = self.buffer_keys
                v2 = self.buffer_value2
        else:
            k = key
            v2 = value2

        #attn_map = q.unsqueeze(-2) * k.unsqueeze(-3)
        # Assuming q.shape is [batch_size, num_heads, seq_length_q, head_dim]
        # and k.shape is [batch_size, num_heads, seq_length_k, head_dim]
        # after unsqueezing
        # q is unsqueezed to add an extra dimension for seq_length_k
        # k is unsqueezed to add an extra dimension for seq_length_q
        #print(q.shape)
        #exit(0)
        ##############################
        #print(q.shape)
        #print(k.shape)
        #q_expanded = q.unsqueeze(-2)  # Add dimension for seq_length_k, preparing for broadcasting
        #k_expanded = k.unsqueeze(-3)  # Add dimension for seq_length_q, preparing for broadcasting

        # Compute element-wise mean
        #mean_qk = (q_expanded + k_expanded) / 2

        # Compute cross-variance
        #cross_variance = ((q_expanded - mean_qk) ** 2 + (k_expanded - mean_qk) ** 2)/2
        #print(cross_variance.shape)
        #exit(0)
        ##################
        # Compute cosine similarity between query and key
        cos_sim = F.cosine_similarity(q.unsqueeze(3), k.unsqueeze(2), dim=-1)  # Shape: (50, 8, 17, 17)
        # Reshape for broadcasting
        cos_sim_exp = cos_sim.unsqueeze(-1)  # Shape: (50, 8, 17, 17, 1)
        q_dir = F.normalize(q, dim=-1).unsqueeze(3)  # Shape: (50, 8, 17, 1, 64)

        # Final projection: each k projected onto the direction of q, scaled by cosine similarity
        score = cos_sim_exp @ q_dir  # Shape: (50, 8, 17, 17, 64)
        ##################
        attn_map = self.dropout(score)
        attn = self.attn_net.forward(attn_map, mask, v1, v2).transpose(1, 2).contiguous()
        attn = attn.view(batch_size, -1, self.num_heads * self.head_dim)
        return attn

    def precompute(self, key, value2):
        batch_size = value2.size()[0]
        key = key.view(-1, key.size()[-1])
        value2 = value2.view(-1, value2.size()[-1])

        k = self.in_proj_k(key)
        v2 = self.in_proj_v2(value2)

        k = k.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        v2 = v2.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)

        return k, v2

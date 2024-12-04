import torch
from typing import List, Tuple, Dict
from dataclasses import dataclass
from copy import deepcopy
from collections import deque
from tqdm import tqdm

def pad_path(path, length, pad_value=-1):
    return path + [pad_value] * (length - len(path))

class DynSAM:
   
    @dataclass
    class SAMState:
        next: dict[int, int]
        link: int
        length: int
        min_endpos: int

    def __init__(self, 
        max_predicts: int = 40, 
        alpha: float = 4.0, 
        device: str = "cuda"
    ):
        self.max_predicts = max_predicts
        self.alpha = alpha
        self.states: List[DynSAM.SAMState] = [DynSAM.SAMState(next={}, link=-1, length=0, min_endpos=0)]
        self.input_ids: List[int] = [-1]
        self.last = 0
        self.max_length = 0
        self.device = device
        
        # params needed to be reset for each query
        self.cur_index = 0
        self.cur_length = 0
    
    def reset(self):
        self.states: List[DynSAM.SAMState] = [DynSAM.SAMState(next={}, link=-1, length=0, min_endpos=0)]
        self.input_ids: List[int] = [-1]
        self.last = 0
        self.max_length = 0
        self.cur_index = 0
        self.cur_length = 0
    
    def expand_state(self, state: SAMState):
        new_index = len(self.states)
        self.states.append(state)
        return new_index

    def add_state(self, token: int):
        self.max_length += 1
        cur = self.expand_state(
            DynSAM.SAMState(
                next={}, link=-1, 
                length=self.max_length, 
                min_endpos=self.max_length
            )
        )
        p = self.last
        while p != -1 and token not in self.states[p].next:
            self.states[p].next[token] = cur
            p = self.states[p].link
        if p == -1:
            self.states[cur].link = 0
        else:
            q = self.states[p].next[token]
            if self.states[p].length + 1 == self.states[q].length:
                self.states[cur].link = q
            else:
                clone = self.expand_state(deepcopy(self.states[q]))
                self.states[clone].length = self.states[p].length + 1
                while p != -1 and self.states[p].next[token] == q:
                    self.states[p].next[token] = clone
                    p = self.states[p].link
                self.states[q].link = self.states[cur].link = clone
        self.last = cur
           
    def transfer_state(self, index: int, length: int, token: int):
        while index != 0 and token not in self.states[index].next:
            index = self.states[index].link
            length = self.states[index].length
        if token in self.states[index].next:
            index = self.states[index].next[token]
            length += 1
        else:
            index = length = 0
        return index, length
    
    def transfer_cur_state(self, token: int):
        self.cur_index, self.cur_length = \
            self.transfer_state(self.cur_index, self.cur_length, token)
    
    def to_anc(self, index: int, length: int):
        length_to_end = self.max_length - self.states[index].min_endpos
        while index != 0 and self.max_predicts > length_to_end:
            index = self.states[index].link
            length = self.states[index].length
            length_to_end = self.max_length - self.states[index].min_endpos
        return index, length
    
    def add_tokens(self, tokens: List[int]):
        for token in tokens:
            self.transfer_cur_state(token)
            self.add_state(token)
        self.input_ids.extend(tokens)
    
    def transfer_tokens(self, tokens: List[int]):
        for token in tokens:
            self.transfer_cur_state(token)

    def lookup(self, token: int):
        index, length = \
            self.transfer_state(self.cur_index, self.cur_length, token)
        return index, length

    def gen_draft(self, index: int, match_length: int, start_token: int):
        n = min(self.max_predicts, 1 + int(match_length * self.alpha))
        endpos = self.states[index].min_endpos
        seq = [start_token] + self.input_ids[endpos + 1:endpos + n]
        seq_position_ids = torch.arange(0, len(seq), dtype=torch.long, device=self.device).unsqueeze(0)
        return seq, {"seq_position_ids": seq_position_ids}

    def gen_buffers(self, anc_tree: List[int]):
        n = len(anc_tree)
        is_leaf = [True] * n
        tree_position_ids = [0] * n
        for i in range(1, n):
            is_leaf[anc_tree[i]] = False
            tree_position_ids[i] = tree_position_ids[anc_tree[i]] + 1
        tree_position_ids = torch.tensor([tree_position_ids], dtype=torch.long, device=self.device)
        
        tree_attn_mask = torch.zeros((n, n), dtype=torch.bool)
        for i in range(n):
            j = i
            while j != -1:
                tree_attn_mask[i, j] = True
                j = anc_tree[j]
        tree_attn_mask = tree_attn_mask.view(1, 1, n, n).to(self.device)
        
        retrieve_indices_nest = []
        for i in range(n):
            if not is_leaf[i]:
                continue
            retrieve_indices = [i]
            while retrieve_indices[-1] != 0:
                retrieve_indices.append(anc_tree[retrieve_indices[-1]])
            retrieve_indices_nest.append(list(reversed(retrieve_indices)))
        max_depth = max(len(x) for x in retrieve_indices_nest)
        retrieve_indices_nest = [pad_path(x, max_depth) for x in retrieve_indices_nest]
        tree_retrieve_indices = torch.tensor(retrieve_indices_nest, dtype=torch.long, device=self.device)
        return {
            "tree_attn_mask": tree_attn_mask,
            "tree_position_ids": tree_position_ids,
            "tree_retrieve_indices": tree_retrieve_indices,
        }
    
    def gen_tree_draft(self, index: int, match_length: int, start_token: int):
        n = min(self.max_predicts, 1 + int(match_length * self.alpha))
        h: List[Tuple[int, int, int]] = []
        tree = []
        anc_tree = []
        h.append((index, -1, start_token))
        while len(tree) != n and len(h) != len(tree):
            cur_tree_index = len(tree)
            cur_index, anc_tree_index, cur_token = h[cur_tree_index]
            tree.append(cur_token)
            anc_tree.append(anc_tree_index)
            if len(tree) == n:
                break
            for n_token, n_index in self.states[cur_index].next.items():
                h.append((n_index, cur_tree_index, n_token))
        return tree, self.gen_buffers(anc_tree)

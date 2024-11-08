import torch
from typing import List, Tuple, Dict, Optional
from enum import Enum
from collections import namedtuple

from .samd_config import SamdConfig
from .sam import DynSAM, StaticSAM
from .tree_model import TreeModel, tree_model_cls
from profile_utils import profile_decorator
from transformers import LlamaConfig, LlamaForCausalLM

# from transformers import LlamaTokenizer
# tokenizer: LlamaTokenizer = LlamaTokenizer.from_pretrained('/data/models/vicuna-7b-v1.3')

class CandidateType(str, Enum):
    sequence = "sequence"
    tree = "tree"

Candidates = namedtuple('Candidates', ['type', 'tokens', 'candidate_tokens'])

TOPK = 8

class DraftModel(torch.nn.Module):
    
    def __init__(self,
        config: SamdConfig,
        sam_dyn: DynSAM = None,
        sam_static: StaticSAM = None,
        tree_model: TreeModel = None,
        lm: LlamaForCausalLM = None,
        dtype: torch.dtype = torch.float16,
        device: str = "cuda",
    ) -> None:
        super().__init__()
        tree_cls = tree_model_cls[config.tree_method]
        self.config = config
        self.sam_dyn = sam_dyn if sam_dyn is not None else DynSAM(config.n_predicts)
        self.sam_static = sam_static if sam_static is not None else StaticSAM(config.n_predicts)
        self.tree_model = tree_model if tree_model is not None else tree_cls(config, lm, dtype, device)
        
        self.sam_dyn.n_predicts = config.n_predicts
        self.sam_static.n_predicts = config.n_predicts
        self.len_bias = config.len_bias
        self.len_threshold = config.len_threshold
        
    def reset(self):
        self.sam_dyn.reset()
        self.sam_static.reset()
        self.tree_model.reset()

    def lookup(self, start_token: int):
        pred_dyn, match_dyn = self.sam_dyn.lookup(start_token)
        pred_static, match_static = self.sam_static.lookup(start_token)
        match_static -= self.len_bias
        if match_dyn >= match_static:
            pred, len = pred_dyn, match_dyn
        else:
            pred, len = pred_static, match_static        
        if len >= self.len_threshold:
            return CandidateType.sequence, [start_token] + pred
        else:
            return CandidateType.tree, self.tree_model.lookup(start_token)
    
    @profile_decorator("DraftModel.update")
    def update(self, 
        tokens: Optional[torch.Tensor] = None,
        last_hidden_states: Optional[torch.Tensor] = None,
        tree_tokens: Optional[torch.Tensor] = None,
        tree_logits: Optional[torch.Tensor] = None,
    ):
        tokens_list = tokens.tolist()
        self.sam_dyn.add_tokens(tokens_list)
        self.sam_static.transfer_tokens(tokens_list)
        self.tree_model.update(
            tokens=tokens,
            last_hidden_states=last_hidden_states,
            tree_tokens=tree_tokens, 
            tree_logits=tree_logits,
        )

"""Benchmark offline inference throughput."""
import string
import argparse
import json
import random
import time
from typing import List, Tuple

import torch
from transformers import (AutoConfig, AutoTokenizer, AutoModelForCausalLM,
                          PreTrainedTokenizerBase)
from tqdm import tqdm

from vllm import LLM, SamplingParams

def seed_everything(seed: int):
    import random, os
    import numpy as np
    import torch
    
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    
seed_everything(42)

def get_tokenizer(model_name: str) -> PreTrainedTokenizerBase:
    config = AutoConfig.from_pretrained(model_name)
    if config.model_type == "llama":
        # A workaround for potential protobuf errors.
        model_name = "hf-internal-testing/llama-tokenizer"
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        # To enable padding in the HF backend.
        tokenizer.pad_token = tokenizer.eos_token
        return tokenizer
    return AutoTokenizer.from_pretrained(model_name)

# def sample_requests(
#     dataset_path: str,
#     num_requests: int,
#     tokenizer: PreTrainedTokenizerBase,
# ) -> List[Tuple[str, int, int]]:
#     # Load the dataset.
#     with open(dataset_path) as f:
#         dataset = json.load(f)
#     # Filter out the conversations with less than 2 turns.
#     dataset = [
#         data for data in dataset
#         if len(data["conversations"]) >= 2
#     ]
#     # Only keep the first two turns of each conversation.
#     dataset = [
#         (data["conversations"][0]["value"], data["conversations"][1]["value"])
#         for data in dataset
#     ]

#     # Tokenize the prompts and completions.
#     prompts = [prompt for prompt, _ in dataset]
#     prompt_token_ids = tokenizer(prompts).input_ids
#     completions = [completion for _, completion in dataset]
#     completion_token_ids = tokenizer(completions).input_ids
#     tokenized_dataset = []
#     for i in range(len(dataset)):
#         output_len = len(completion_token_ids[i])
#         tokenized_dataset.append((prompts[i], prompt_token_ids[i], output_len))

#     # Filter out too long sequences.
#     filtered_dataset: List[Tuple[str, int, int]] = []
#     for prompt, prompt_token_ids, output_len in tokenized_dataset:
#         prompt_len = len(prompt_token_ids)
#         if prompt_len < 4 or output_len < 4:
#             # Prune too short sequences.
#             continue
#         if prompt_len > 1024 or prompt_len + output_len > 2048:
#             # Prune too long sequences.
#             continue
#         filtered_dataset.append((prompt, prompt_len, output_len))

#     # Sample the requests.
#     sampled_requests = random.sample(filtered_dataset, num_requests)
#     return sampled_requests

def sample_requests(
    dataset_path: str,
    num_requests: int,
    tokenizer: PreTrainedTokenizerBase,
) -> List[Tuple[str, int, int]]:
    res = []
    for _ in range(num_requests):
        # prompt = ''.join(
        #     random.choices(
        #         string.ascii_uppercase + string.digits,
        #         k=random.randint(args.min_prompt_len, args.max_prompt_len)))
        prompt = '!' * random.randint(args.min_prompt_len, args.max_prompt_len)
        prompt_len = len(tokenizer(prompt).input_ids)
        output_len = random.randint(args.min_response_len, args.max_response_len)
        res.append((prompt, prompt_len, output_len))

    return res

def run_vllm(
    requests: List[Tuple[str, int, int]],
    model: str,
    tensor_parallel_size: int,
    seed: int,
    n: int,
    use_beam_search: bool,
) -> float:
    llm = LLM(
        model=model,
        tensor_parallel_size=tensor_parallel_size,
        seed=seed,
        use_dummy_weights=True,
        max_num_seqs=args.batch_size,
        max_num_batched_tokens=args.batch_size * (args.max_prompt_len + args.max_response_len + 1),
    )

    # Add the requests to the engine.
    for prompt, _, output_len in requests:
        sampling_params = SamplingParams(
            n=n,
            temperature=0.0 if use_beam_search else 1.0,
            top_p=1.0,
            use_beam_search=use_beam_search,
            ignore_eos=True,
            max_tokens=output_len,
        )
        # FIXME(woosuk): Do not use internal method.
        llm._add_request(
            prompt=prompt,
            prompt_token_ids=None,
            sampling_params=sampling_params,
        )

    start = time.time()
    # FIXME(woosuk): Do use internal method.
    llm._run_engine(use_tqdm=True)
    end = time.time()
    return end - start


def run_hf(
    requests: List[Tuple[str, int, int]],
    model: str,
    tokenizer: PreTrainedTokenizerBase,
    n: int,
    use_beam_search: bool,
    max_batch_size: int,
) -> float:
    assert not use_beam_search
    tokenizer = get_tokenizer(model)
    llm = AutoModelForCausalLM.from_pretrained(
        model, torch_dtype=torch.float16)
    llm = llm.cuda()

    pbar = tqdm(total=len(requests))
    start = time.time()
    batch: List[str] = []
    max_prompt_len = 0
    max_output_len = 0
    for i in range(len(requests)):
        prompt, prompt_len, output_len = requests[i]
        # Add the prompt to the batch.
        batch.append(prompt)
        max_prompt_len = max(max_prompt_len, prompt_len)
        max_output_len = max(max_output_len, output_len)
        if len(batch) < max_batch_size and i != len(requests) - 1:
            # Check if we can add more requests to the batch.
            _, next_prompt_len, next_output_len = requests[i + 1]
            if (max(max_prompt_len, next_prompt_len) + max(
                max_output_len, next_output_len)) <= 2048:
                # We can add more requests to the batch.
                continue

        # Generate the sequences.
        input_ids = tokenizer(batch, return_tensors="pt", padding=True).input_ids
        llm_outputs = llm.generate(
            input_ids=input_ids.cuda(),
            do_sample=not use_beam_search,
            num_return_sequences=n,
            temperature=1.0,
            top_p=1.0,
            use_cache=True,
            max_new_tokens=max_output_len,
        )
        # Include the decoding time.
        tokenizer.batch_decode(llm_outputs, skip_special_tokens=True)
        pbar.update(len(batch))

        # Clear the batch.
        batch = []
        max_prompt_len = 0
        max_output_len = 0
    end = time.time()
    return end - start


def main(args: argparse.Namespace):
    print(args)
    random.seed(args.seed)

    # Sample the requests.
    tokenizer = get_tokenizer(args.model)
    requests = sample_requests(args.dataset, args.num_prompts, tokenizer)

    if args.backend == "vllm":
        elapsed_time = run_vllm(
            requests, args.model, args.tensor_parallel_size, args.seed, args.n,
            args.use_beam_search)
    elif args.backend == "hf":
        assert args.tensor_parallel_size == 1
        elapsed_time = run_hf(requests, args.model, tokenizer, args.n,
                              args.use_beam_search, args.hf_max_batch_size)
    else:
        raise ValueError(f"Unknown backend: {args.backend}")
    total_num_tokens = sum(
        prompt_len + output_len
        for _, prompt_len, output_len in requests
    )
    print(f"Throughput: {len(requests) / elapsed_time:.2f} requests/s, "
          f"{total_num_tokens / elapsed_time:.2f} tokens/s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark the throughput.")
    parser.add_argument("--backend", type=str, choices=["vllm", "hf"],
                        default="vllm")
    parser.add_argument("--dataset", type=str, required=True,
                        help="Path to the dataset.")
    parser.add_argument("--model", type=str, default="facebook/opt-125m")
    parser.add_argument("--tensor-parallel-size", "-tp", type=int, default=1)
    parser.add_argument("--n", type=int, default=1,
                        help="Number of generated sequences per prompt.")
    parser.add_argument("--use-beam-search", action="store_true")
    parser.add_argument("--num-prompts", type=int, default=200,
                        help="Number of prompts to process.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--hf-max-batch-size", type=int, default=None,
                        help="Maximum batch size for HF backend.")
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--min-prompt-len", type=int, default=128)
    parser.add_argument("--max-prompt-len", type=int, default=256)
    parser.add_argument("--min-response-len", type=int, default=256)
    parser.add_argument("--max-response-len", type=int, default=512)
    args = parser.parse_args()
    if args.backend == "vllm":
        if args.hf_max_batch_size is not None:
            raise ValueError("HF max batch size is only for HF backend.")
    elif args.backend == "hf":
        if args.hf_max_batch_size is None:
            raise ValueError("HF max batch size is required for HF backend.")

    main(args)

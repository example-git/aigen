import logging
import os
import platform
import random
import re
import shutil
import sys
from datetime import datetime
from random import randint
from typing import List, Optional, Union

import torch
from accelerate import Accelerator
from lightning.pytorch.callbacks import ModelPruning, StochasticWeightAveraging
from lightning.pytorch.plugins import DeepSpeedPrecisionPlugin
from lightning.pytorch.strategies import StrategyRegistry
from lightning.pytorch.trainer import Trainer
from peft import PeftConfig, PeftModel, prepare_model_for_int8_training
from petals import AutoDistributedModelForCausalLM
from pkg_resources import resource_filename
from tqdm.auto import trange
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    GenerationConfig,
    GPT2Config,
    GPT2LMHeadModel,
    GPT2TokenizerFast,
    PreTrainedTokenizerFast,
    StoppingCriteria,
    StoppingCriteriaList,
)

from .colab import create_gdrive_folder
from .TokenDataset import TokenDataset
from .train import AIGProgressBar, AIGTrainer
from .utils import find_index_of_subset, model_max_length, reset_seed, set_seed

logger = logging.getLogger("aigen")
logger.setLevel(logging.INFO)

STATIC_PATH = resource_filename(__name__, "static")


class aigen:
    """
    Class that serves as the main aigen object for training and generation.

    :param model: Either the file path of a PyTorch GPT-2 model, or a string
    representing the Huggingface model to download.
    :param config: Either a file path of a config.json representing the model,
    or a GPT2Config with the model architecture.
    :param vocab_file: Path to a vocab file (generated by train_tokenizer())
    :param merges_file: Path to a merges file (generated by train_tokenizer())
    :param cache_dir: folder path which downloaded models will be stored and loaded
    :param verbose: Whether to enable logging from base Huggingface packages
    :param bos_token: String to override the beginning-of-string token
    :param eos_token: String to override the end-of-string token
    :param unk_token: String to override the unknown token
    """

    # default values for GPT2Tokenizer
    tokenizer = None
    vocab_file = os.path.join(STATIC_PATH, "gpt2_vocab.json")
    merges_file = os.path.join(STATIC_PATH, "gpt2_merges.txt")
    bos_token = "<|endoftext|>"
    eos_token = "<|endoftext|>"
    unk_token = "<|endoftext|>"
    pad_token = "<|endoftext|>"

    def __init__(
        self,
        model: str = None,
        model_folder: str = None,
        config: Union[str, GPT2Config] = None,
        vocab_file: str = None,
        merges_file: str = None,
        tokenizer_file: str = None,
        schema_tokens: List[str] = None,
        schema_return: List[str] = None,
        cache_dir: str = "aigen",
        embeddings_dir: str = "",
        precision: int = None,
        gradient_checkpointing: bool = False,
        petals: bool = False,
        bos_token: str = None,
        eos_token: str = None,
        unk_token: str = None,
        adapters=None,
        tuning_mode=None,
        pre_seq_len=24,
        **kwargs,
    ) -> None:
        self.mode = "transformer"
        self.memory = None
        self.precision = precision
        self.petals = petals

        qargs = dict(torch_dtype=torch.float32)
        if precision in [16, 8, 4]:
            qargs["torch_dtype"] = torch.bfloat16

        if precision in [8, 4]:
            qargs["llm_int8_has_fp16_weight"] = True
            qargs["llm_int8_threshold"] = 6
            qargs["llm_int8_skip_modules"] = [
                "lm_head",
                "head",
                "pre_ln",
                "ln1",
                "ln2",
                "ln_1",
                "ln_2",
                "ln_f",
                "ln_out",
                "input_layernorm",
                "post_attention_layernorm",
                "final_layer_norm",
                "embed_out",
            ]

        if precision == 8:
            qargs["load_in_8bit"] = True

        if precision == 4:
            qargs["load_in_4bit"] = True
            qargs["bnb_4bit_quant_type"] = "nf4"
            qargs["bnb_4bit_use_double_quant"] = False
            qargs["bnb_4bit_compute_dtype"] = torch.bfloat16

        if config:
            # Manually construct a model from scratch
            logger.info("Constructing model from provided config.")
            if isinstance(config, str):
                config = AutoConfig.from_pretrained(config)
            self.model = AutoModelForCausalLM.from_config(config=config)
        else:
            if model_folder:
                # A folder is provided containing pytorch_model.bin and config.json
                assert os.path.exists(
                    os.path.join(model_folder, "pytorch_model.bin")
                ), f"There is no pytorch_model.bin in /{model_folder}."
                assert os.path.exists(
                    os.path.join(model_folder, "config.json")
                ), f"There is no config.json in /{model_folder}."

                logger.info(
                    f"Loading model from provided weights and config in /{model_folder}."
                )
            else:
                # Download and cache model from Huggingface
                if os.path.isdir(cache_dir) and len(os.listdir(cache_dir)) > 0:
                    logger.info(f"Loading {model} model from {cache_dir}.")
                else:
                    logger.info(f"Downloading {model} model to {cache_dir}.")

            if self.petals:
                print("loading model from Petals")
                self.model = AutoDistributedModelForCausalLM.from_pretrained(
                    model if not model_folder else model,
                    pre_seq_len=pre_seq_len,
                    tuning_mode=tuning_mode,
                    cache_dir=cache_dir,
                    device_map="auto",
                    **qargs,
                )
                embeddings_path = embeddings_dir + "/prompts.pt"
                if tuning_mode:
                    if os.path.exists(embeddings_path):
                        with open(embeddings_path, "rb") as f:
                            if torch.cuda.is_available():
                                self.model.transformer.prompt_embeddings = torch.load(f)
                                if tuning_mode == "deep_ptune":
                                    self.model.transformer.intermediate_prompt_embeddings = torch.load(
                                        f
                                    )
                            else:
                                self.model.transformer.prompt_embeddings = torch.load(
                                    f, map_location=torch.device("cpu")
                                )
                                if tuning_mode == "deep_ptune":
                                    self.model.transformer.intermediate_prompt_embeddings = torch.load(
                                        f, map_location=torch.device("cpu")
                                    )
            else:
                self.model = AutoModelForCausalLM.from_pretrained(
                    model if not model_folder else model,
                    cache_dir=cache_dir,
                    trust_remote_code=True,
                    local_files_only=True if model_folder else False,
                    device_map="auto",
                    **qargs,
                )
            logger.info(f"Using the tokenizer for {model}.")
            self.tokenizer = AutoTokenizer.from_pretrained(
                model,
                cache_dir=cache_dir,
                padding_side="left",
                padding=False,
                truncation=False,
                add_prefix_space=False,
            )

        if adapters and not petals:
            for adapter in adapters:
                peft_config = PeftConfig.from_pretrained(adapter)
                peft_config.init_lora_weights = False
                logger.info(f"Using adapter: {adapter}")
                self.model.add_adapter(peft_config, adapter_name=adapter)
            self.model.enable_adapters()

        self.model_max_length = model_max_length(self.model.config)

        self.model = self.model.eval()
        logger.info(self)

        if gradient_checkpointing:
            logger.info("Gradient checkpointing enabled for model training.")
            self.model.gradient_checkpointing_enable()
            setattr(self.model.config, "use_cache", None if petals else False)

        if schema_tokens:
            setattr(self.model.config, "schema_tokens", schema_tokens)

        if schema_return:
            setattr(self.model.config, "schema_return", schema_return)

        if self.tokenizer is None:
            # Update tokenizer settings (if not set already)
            args = locals()
            custom_tokenizer = False
            for attr in [
                "vocab_file",
                "merges_file",
                "tokenizer_file",
                "bos_token",
                "eos_token",
                "unk_token",
            ]:
                if args[attr] is not None:
                    custom_tokenizer = True
                    setattr(self, attr, args[attr])

            if custom_tokenizer:
                logger.info("Using a custom tokenizer.")
            else:
                logger.info("Using the default tokenizer.")

            if tokenizer_file:
                # load the custom GPT-2 tokenizer from a serialized tokenizer.
                # GPT-Neo uses the GPT-2 tokenizer.
                self.tokenizer = PreTrainedTokenizerFast(
                    tokenizer_file=tokenizer_file,
                    bos_token=self.bos_token,
                    eos_token=self.eos_token,
                    unk_token=self.unk_token,
                    pad_token=self.pad_token,
                    padding_side="left",
                )
            else:
                self.tokenizer = GPT2TokenizerFast(
                    vocab_file=self.vocab_file,
                    merges_file=self.merges_file,
                    bos_token=self.bos_token,
                    eos_token=self.eos_token,
                    unk_token=self.unk_token,
                    pad_token=self.pad_token,
                    verbose=False,
                    padding_side="left",
                    add_prefix_space=False,
                )
                if not custom_tokenizer:
                    # https://github.com/huggingface/transformers/issues/10202
                    self.tokenizer.add_special_tokens(
                        {"additional_special_tokens": ["<|endoftext|>"]}
                    )

    def generate(
        self,
        prompt: str = "",
        prepend_bos: bool = None,
        min_length: int = None,
        max_new_tokens: int = None,
        temperature: float = 0.7,
        do_sample: bool = True,
        seed: int = None,
        schema: str = False,
        normalize_key: bool = True,
        use_cache: bool = True,
        lstrip: bool = True,
        nonempty_output: bool = True,
        skip_special_tokens: bool = True,
        mode: str = "transformer",
        **kwargs,
    ) -> Optional[str]:
        """
        Generates texts using the stored Transformers model.
        Currently generates text using the model's generate() function.

        :param n: Numbers of texts to generate.
        :param prompt: Text to force the generated text to start with
        :param temperature: Determines the "creativity" of the generated text.
        The value range is different for each type of Transformer.
        :param do_sample: Samples the text, which is what we want. If False,
        the generated text will be the optimal prediction at each time,
        and therefore deterministic.
        :param seed: A numeric seed which sets all randomness, allowing the
        generate text to be reproducible if rerunning with same parameters
        and model.
        """

        prompt_tensors = self.tokenizer(text=prompt, return_tensors="pt")

        if prompt:
            prompt_num_tokens = list(prompt_tensors["input_ids"].shape)[1]
            assert prompt_num_tokens < model_max_length(
                self.model.config
            ), f"The prompt is too large for the model. ({prompt_num_tokens} tokens)"

        input_ids = (
            prompt_tensors["input_ids"].to(self.get_device()) if prompt else None
        )

        if prepend_bos is None:
            prepend_bos = getattr(self.model.config, "line_by_line", None)

        if prepend_bos:
            bos = torch.tensor([[self.tokenizer.bos_token_id]]).to(self.get_device())
            if prompt:
                input_ids = torch.cat((bos, input_ids), dim=1)
            else:
                input_ids = bos

        if seed:
            set_seed(seed)

        self.mode = mode
        if mode in ["rnn"]:
            torch.set_grad_enabled(False)
            inputs = prompt_tensors["input_ids"].to(self.get_device())
            if self.memory is not None:
                self.memory = self.model(
                    inputs,
                    state=self.memory,
                ).state
            else:
                self.memory = self.model(inputs).state
            # print(self.memory[0][:, -2])

        # config = GenerationConfig(
        #     do_sample=do_sample,
        #     **kwargs,
        # )

        while True:
            outputs = self.model.generate(
                inputs=input_ids,
                # generation_config=config,
                do_sample=do_sample,
                max_new_tokens=max_new_tokens,
                use_cache=use_cache,
                return_dict_in_generate=True,
                output_hidden_states=False,
                output_attentions=False,
                output_scores=False,
                state=self.memory,
                **kwargs,
            )

            gen_texts = self.tokenizer.batch_decode(
                outputs["sequences"], skip_special_tokens=skip_special_tokens
            )

            # Handle stripping tokenization spaces w/ regex
            if lstrip:
                gen_texts = [re.sub(r"^\s+", "", text) for text in gen_texts]

            if nonempty_output:
                if min_length:
                    gen_texts = list(filter(lambda x: len(x) > min_length, gen_texts))
                else:
                    gen_texts = list(filter(lambda x: len(x) > 0, gen_texts))

            # if there is no generated text after cleanup, try again.
            if len(gen_texts) == 0:
                continue

            # Reset seed if used
            if seed:
                reset_seed()

            return gen_texts[0]

            # Schema token handling
            # if schema:
            #     schema_tokens = getattr(self.model.config, "schema_tokens")
            #     schema_return = getattr(self.model.config, "schema_return", None)
            #     schema_tokens_enc = self.tokenizer(text=schema_tokens)["input_ids"]

            #     nonalphanum_pattern = re.compile(r"[\W_]+", re.UNICODE)

            #     outputs = outputs.tolist()
            #     gen_texts = []
            #     for output in outputs:
            #         gen_text_dict = {}

            #         # Get indices of each schema token within the text
            #         schema_token_indices = [
            #             (schema_tokens[i], find_index_of_subset(output, token_enc))
            #             for i, token_enc in enumerate(schema_tokens_enc)
            #         ]

            #         schema_token_indices.sort(key=lambda x: x[1])

            #         for i, token_tuple in enumerate(schema_token_indices):
            #             start_index = token_tuple[1]
            #             key = (
            #                 nonalphanum_pattern.sub("", token_tuple[0])
            #                 if normalize_key
            #                 else token_tuple[0]
            #             )
            #             if start_index == -1:
            #                 gen_text_dict[key] = ""
            #             else:
            #                 end_index = (
            #                     schema_token_indices[i + 1][1] - 1
            #                     if i + 1 < len(schema_token_indices)
            #                     else None
            #                 )

            #                 gen_text_dict[key] = self.tokenizer.decode(
            #                     output[start_index:end_index], skip_special_tokens=True
            #                 )

            #         # remove fields not in schema_return
            #         if schema_return:
            #             keys = gen_text_dict.keys()
            #             if len(schema_return) == 1:
            #                 gen_text_dict = gen_text_dict[schema_return[0]]
            #             for key in keys:
            #                 if key not in schema_return:
            #                     gen_text_dict.pop(key, None)

            #         gen_texts.append(gen_text_dict)

            #     return gen_texts[0]

            # # Typical use case
            # else:

    def train(
        self,
        train_data: Union[str, TokenDataset],
        output_dir: str = "trained_model",
        n_gpu: int = -1,
        tpu_cores: int = 0,
        gradient_clip_val: float = 0.5,
        gradient_accumulation_steps: int = 1,
        seed: int = None,
        optimizer: str = "AdamW",
        learning_rate: float = 1e-3,
        swa_lr: float = None,
        update_period: int = 10,
        weight_decay: float = 0.05,
        adam_epsilon: float = 1e-8,
        warmup_steps: int = 0,
        num_steps: int = 5000,
        save_every: int = 1000,
        generate_every: int = 1000,
        n_generate: int = 1,
        loggers: List = None,
        batch_size: int = 1,
        num_workers: int = None,
        benchmark: bool = True,
        avg_loss_smoothing: float = 0.01,
        save_gdrive: bool = False,
        run_id: str = f"ATG_{datetime.utcnow():%Y%m%d_%H%M%S}",
        progress_bar_refresh_rate: int = 20,
        num_layers_freeze: int = None,
        use_deepspeed: bool = False,
        scheduler: str = "get_linear_schedule_with_warmup",
        prune: float = 0.0,
        petals: bool = False,
        hivemind: bool = False,
        target_batch_size: int = 8192,
        **kwargs,
    ) -> None:
        """
        Trains/finetunes the model on the provided file/dataset using pytorch-lightning.

        :param train_data: Either a TokenDataset containing the samples to be trained, or
        a string containing the text to be trained (shortcut instead of dataset)
        :param output_dir: A string indicating where to store the resulting
        model file folder.
        :param n_gpu: Number of GPU to use (-1 implies all available GPUs)
        :param tpu_cores: Number of TPU cores to use (should be a multiple of 8)
        :param gradient_clip_val: Maximum gradient normalization
        :param gradient_accumulation_steps: Number of gradient acc steps
        :param seed: Interger representing the training seed.
        :param learning_rate: Training learning rate for the default AdamW optimizer.
        :param weight_decay: Weight decay for the default AdamW optimizer.
        :param warmup_steps: Warmrup steps for the default AdamW optimizer.
        :param num_steps: Number of samples through the dataset.
        :param save_every: Number of steps for each time to save the model to disk
        :param generate_every: Number of steps for each time to generate sample text
        :param n_generate: Number of texts to generate when generate_every occurs.
        :param loggers: pytorch-lightning logger(s) to log results.
        :param batch_size: Number of input samples per batch
        :param num_workers: Number of DataLoader workers
        :param benchmark: If using GPU, whether to use cudnn.benchmarkl
        :param avg_loss_smoothing: Smoothing factor for Avg loss in progress bar
        :param save_gdrive: If using Colab, whether to save the notebook
        to Google Drive at each save_every
        :param run_id: Run identifier; used for save_gdrive
        :param progress_bar_refresh_rate: How often to update
        the progress bar while training.
        """

        self.petals = petals

        if num_layers_freeze is not None:
            freeze_layers = True

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        if save_gdrive:
            assert (
                "google.colab" in sys.modules
            ), "You must be in Colaboratory to copy to your Google Drive"
            create_gdrive_folder(run_id)

        self.model = self.model.train()
        is_gpu_used = torch.cuda.is_available() and n_gpu != 0

        if hasattr(self.model, "enable_input_require_grads"):
            self.model.enable_input_require_grads()
        else:

            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)

            self.model.get_input_embeddings().register_forward_hook(
                make_inputs_require_grad
            )

        if isinstance(train_data, str):
            block_size = model_max_length(self.model.config)
            logger.info(
                f"Loading text from {train_data} with generation length of {block_size}."
            )
            train_data = TokenDataset(
                tokenizer=self.tokenizer,
                bos_token=self.bos_token,
                eos_token=self.eos_token,
                unk_token=self.unk_token,
                file_path=train_data,
                block_size=block_size,
                **kwargs,
            )

        setattr(self.model.config, "line_by_line", train_data.line_by_line)

        if freeze_layers:
            logger.info("Layer freezing enabled for model training.")
            freeze_layers = True
            if num_layers_freeze:
                # For GPT-2
                if hasattr(self.model.config, "n_layer"):
                    assert (
                        num_layers_freeze < self.model.config.n_layer
                    ), "You are freezing more Transformer layers than in the model."
                # For GPT-Neo
                elif hasattr(self.model.config, "num_layers"):
                    assert (
                        num_layers_freeze < self.model.config.num_layers
                    ), "You are freezing more Transformer layers than in the model."
                # For RWKV
                elif hasattr(self.model.config, "num_hidden_layers"):
                    assert (
                        num_layers_freeze < self.model.config.num_hidden_layers
                    ), "You are freezing more Transformer layers than in the model."

        if num_workers is None:
            # Use all CPU cores as workers if not training on CPU
            if is_gpu_used or tpu_cores > 0:
                num_workers = os.cpu_count()
            # If training on the CPU, use half the CPUs
            else:
                num_workers = int(os.cpu_count() / 2)

        hparams = dict(
            optimizer=optimizer,
            learning_rate=learning_rate,
            update_period=update_period,
            weight_decay=weight_decay,
            adam_epsilon=adam_epsilon,
            warmup_steps=warmup_steps,
            batch_size=batch_size,
            num_steps=num_steps,
            pin_memory=is_gpu_used,
            num_workers=num_workers,
            save_every=save_every,
            generate_every=generate_every,
            use_tpu=tpu_cores > 0,
            scheduler=scheduler,
            petals=petals,
            hivemind=hivemind,
        )

        # Wrap the model in a pytorch-lightning module
        train_model = AIGTrainer(
            self.model,
            train_data,
            hparams,
            self.tokenizer,
        )

        # Begin training
        if seed:
            set_seed(seed)

        if os.path.exists(output_dir) and "pytorch_model.bin" in os.listdir(output_dir):
            logger.warning(
                f"pytorch_model.bin already exists in /{output_dir} and will be overwritten!"
            )

        # if try to use a GPU but no CUDA, use CPU
        if not is_gpu_used:
            n_gpu = 0

        # force single-GPU on Windows
        if platform.system() == "Windows" and is_gpu_used and n_gpu != 1:
            logger.warning(
                "Windows does not support multi-GPU training. Setting to 1 GPU."
            )
            n_gpu = 1

        # use the DeepSpeed plugin if installed and specified
        # deepspeed_plugin = None
        # if is_gpu_used and use_deepspeed:
        #     deepspeed_plugin = DeepSpeedPrecisionPlugin(
        #         "16-mixed" if fp16 else "32-true"
        #     )
        #     logger.info("Using DeepSpeed training.")
        #     if not fp16:
        #         logger.info("Setting FP16 to True for DeepSpeed ZeRO Training.")
        #         fp16 = True

        # accelerator = Accelerator(
        #     cpu=False, mixed_precision="fp16" if self.precision in [4, 8, 16] else "no"
        # )

        train_params = dict(
            accelerator="auto",
            devices=n_gpu,
            max_steps=num_steps,
            enable_checkpointing=False,
            # precision="bf16-mixed" if self.precision in [4, 8, 16] else 32,
            precision=32,
            logger=loggers if loggers else False,
            callbacks=[
                AIGProgressBar(
                    save_every,
                    generate_every,
                    output_dir,
                    n_generate,
                    is_gpu_used,
                    avg_loss_smoothing,
                    run_id,
                    save_gdrive,
                    progress_bar_refresh_rate,
                    freeze_layers,
                    num_layers_freeze,
                    petals,
                    hivemind,
                )
            ],
            # plugins=deepspeed_plugin,
        )

        if hparams["optimizer"] not in ["SophiaH"]:
            train_params["gradient_clip_val"] = gradient_clip_val
            train_params["gradient_clip_algorithm"] = "norm"

        if tpu_cores > 0:
            train_params["tpu_cores"] = tpu_cores
            train_params["devices"] = 0
            n_gpu = 0

        # benchmark gives a boost for GPUs if input size is constant,
        # which will always be the case with aigen training
        if is_gpu_used and benchmark:
            train_params["benchmark"] = True

        if n_gpu > 1:
            train_params["strategy"] = "ddp"

        if prune > 0.0:
            train_params["callbacks"].append(
                ModelPruning(
                    pruning_fn="l1_unstructured",
                    amount=prune,
                    use_global_unstructured=True,
                    apply_pruning=True,
                    make_pruning_permanent=True,
                    use_lottery_ticket_hypothesis=False,
                )
            )

        if swa_lr:
            train_params["callbacks"].append(StochasticWeightAveraging(swa_lrs=swa_lr))

        if hivemind:
            try:
                from lightning_hivemind.strategy import HivemindStrategy
            except ImportError:
                print("Failed to import HivemindStrategy. Is it installed?")
            train_params["strategy"] = HivemindStrategy(
                target_batch_size=target_batch_size, verbose=True
            )
        else:
            train_params["accumulate_grad_batches"] = gradient_accumulation_steps

        trainer = Trainer(**train_params)
        trainer.fit(train_model)

        if not petals:
            logger.info(f"Saving trained model pytorch_model.bin to /{output_dir}")
            self.model.save_pretrained(output_dir)

        if save_gdrive:
            for pt_file in ["pytorch_model.bin", "config.json"]:
                shutil.copyfile(
                    os.path.join(output_dir, pt_file),
                    os.path.join("/content/drive/MyDrive/", run_id, pt_file),
                )

        if seed:
            reset_seed()

    def save(self, target_folder: str = os.getcwd()):
        """Saves the model into the specified directory."""
        self.model.save_pretrained(target_folder)

    def save_for_upload(self, target_folder: str = "my-model"):
        """
        Saves the model + tokenizerinto the specified directory.

        This generates the 6 files needed to upload the model to
        Huggingface's S3 bucket.
        """
        self.model.save_pretrained(target_folder)
        self.tokenizer.save_pretrained(target_folder)

    def get_device(self) -> str:
        """Getter for the current device where the model is located."""
        return self.model.device.type

    # This controls the output of the aigen object, when printed to console.
    def __repr__(self) -> str:
        # https://discuss.pytorch.org/t/how-do-i-check-the-number-of-parameters-of-a-model/4325/24
        num_params_m = int(sum(p.numel() for p in self.model.parameters()) / 10**6)
        model_name = type(self.model.config).__name__.replace("Config", "")
        return f"{model_name} loaded with {num_params_m}M parameters."


class SingleStoppingCriteria(StoppingCriteria):
    def __init__(self, tokenizer, target_sequence, prompt):
        self.target_sequence = target_sequence
        self.prompt = prompt
        self.tokenizer = tokenizer

    def __call__(self, input_ids, scores, **kwargs):
        # Get the generated text as a string
        generated_text = self.tokenizer.decode(input_ids[0])
        generated_text = generated_text.replace(self.prompt, "")
        # Check if the target sequence appears in the generated text
        if self.target_sequence in generated_text:
            return True  # Stop generation

        return False  # Continue generation

    def __len__(self):
        return 1

    def __iter__(self):
        yield self

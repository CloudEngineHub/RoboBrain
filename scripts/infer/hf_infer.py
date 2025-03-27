import argparse
import torch
import os
from PIL import Image
from model.llava_utils.constants import IGNORE_INDEX, IMAGE_TOKEN_INDEX
from model.llava_utils.conversation import conv_templates, SeparatorStyle
from model.llava_builder import load_pretrained_model
from model.llava_utils.utils import disable_torch_init
from model.llava_utils.mm_utils import KeywordsStoppingCriteria
from model.llava_utils.mm_utils import get_model_name_from_path
from typing import Dict

# For command line history and autocompletion.
import sys
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory
prompt_toolkit_available = False
prompt_toolkit_available = sys.stdout.isatty()

conversation_history = []  # chat history

# Initialize the model
def init_model(args):
    disable_torch_init()
    model_name = get_model_name_from_path(args.model_dir)
    assert "qwen" in model_name.lower(), "model_name should contain or 'qwen'"
    model_name = model_name.lower().replace("qwen", "llava_qwen")
    tokenizer, model, image_processor, _ = load_pretrained_model(args.model_dir, None, model_name, torch_dtype='bfloat16')
    return tokenizer, model, image_processor

# Process the multi-turn conversation
def preprocess_qwen(sources, tokenizer, has_image=False, max_len=32768, system_message="You are a helpful assistant."):
    roles = {"human": "<|im_start|>user", "gpt": "<|im_start|>assistant"}
    input_ids, targets = [], []
    input_id, target = [], []
    system = [tokenizer.additional_special_tokens_ids[0]] + tokenizer("system").input_ids \
        + tokenizer(system_message).input_ids + [tokenizer.additional_special_tokens_ids[1]] + tokenizer("\n").input_ids
    input_id += system
    target += [IGNORE_INDEX] * len(system)
    
    for sentence in sources:
        role = roles[sentence["from"]]
        if has_image and "<image>" in sentence["value"]:
            texts = sentence["value"].split("<image>")
            _input_id = tokenizer(role).input_ids + tokenizer("\n").input_ids
            for i, text in enumerate(texts):
                _input_id += tokenizer(text).input_ids
                if i < len(texts) - 1:
                    _input_id += [IMAGE_TOKEN_INDEX] + tokenizer("\n").input_ids
            _input_id += [tokenizer.additional_special_tokens_ids[1]] + tokenizer("\n").input_ids
        else:
            _input_id = tokenizer(role).input_ids + tokenizer("\n").input_ids \
                + tokenizer(sentence["value"]).input_ids + [tokenizer.additional_special_tokens_ids[1]] + tokenizer("\n").input_ids
        input_id += _input_id
        target += [IGNORE_INDEX] * len(_input_id)

    input_ids.append(input_id)
    targets.append(target)
    return torch.tensor(input_ids, dtype=torch.long)

# Generate model responses
def infer_model(args, tokenizer, model, image_processor, user_input):
    global conversation_history
    conversation_history.append({"from": "human", "value": user_input})
    input_ids = preprocess_qwen(conversation_history, tokenizer, has_image=bool(args.image_files)).cuda()
    
    # Load and process images if provided
    image_tensors = []
    image_sizes = []
    modalities = ["image"]
    if args.image_files:  # Check if image files are provided
        image_files = args.image_files.split(",")
        num_image = len(image_files)

        # if the number of images is larger than 8, then we process them as video do.
        if num_image > 8:
            modalities = ["video"] * num_image
        else:
            modalities = ["image"] * num_image

        for image_file in image_files:
            image = Image.open(image_file)
            image_tensor = image_processor.preprocess(image, return_tensors="pt")["pixel_values"]
            image_tensors.append(image_tensor.to(dtype=torch.bfloat16, device="cuda"))
            image_sizes.append(image.size)

    # Set stopping conditions
    conv_mode = conv_templates[args.conv_mode]
    stop_str = conv_mode.sep if conv_mode.sep_style != SeparatorStyle.TWO else conv_mode.sep2
    stopping_criteria = KeywordsStoppingCriteria([stop_str], tokenizer, input_ids)

    # Inference
    with torch.inference_mode():
        output_ids = model.generate(
            input_ids,
            images=image_tensors if image_tensors else None,  # Pass None if no images
            image_sizes=image_sizes if image_sizes else None,  # Pass None if no images
            modalities=modalities, # fix bugs for multi-images
            do_sample=True if args.temperature > 0 else False,
            temperature=args.temperature,
            top_p=args.top_p,
            num_beams=args.num_beams,
            max_new_tokens=args.max_new_tokens,
            use_cache=True,
            stopping_criteria=[stopping_criteria]
        )
    outputs = tokenizer.batch_decode(output_ids, skip_special_tokens=True)[0].strip()
    response = outputs.replace("assistant\n", "")
    response = response.split(stop_str)[0].strip()

    # Append response to history
    conversation_history.append({"from": "gpt", "value": response})
    return response

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir", type=str, default="/path/to/model")
    parser.add_argument("--conv_mode", type=str, default="qwen_2")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--top_p", type=float, default=0.95)
    parser.add_argument("--num_beams", type=int, default=1)
    parser.add_argument("--max_new_tokens", type=int, default=512)
    args = parser.parse_args()

    tokenizer, model, image_processor = init_model(args)

    if prompt_toolkit_available:
        history = InMemoryHistory()

    print("Enter 'exit' to end the conversation, 'reset' to clear the chat history.")

    while True:
        # Prompt for images if history is cleared or at the start of the conversation
        if not conversation_history:
            if prompt_toolkit_available:
                args.image_files = prompt("Enter image file paths (comma-separated): ", history=history, enable_history_search=True)
            else:
                args.image_files = input("Enter image file paths (comma-separated): ")

        if prompt_toolkit_available:
            user_input = prompt("User: ", history=history, enable_history_search=True)
        else:
            user_input = input("User: ")
        
        if user_input.lower() == "exit":
            print("Exiting the conversation.")
            break
        elif user_input.lower() == "reset":
            conversation_history = []
            args.image_files = None
            print("Conversation history reset.")
        else:
            response = infer_model(args, tokenizer, model, image_processor, user_input)
            print("Assistant:", response)

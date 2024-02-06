from t0_config import Project_path, workdata_path
import numpy as np
import os
from tqdm import tqdm
from tqdm.auto import tqdm
from torch.utils.data import DataLoader
import torch
from datasets import load_dataset, load_metric
from transformers import (
    Seq2SeqTrainingArguments,
    AutoTokenizer, AutoModelForSeq2SeqLM,
    DataCollatorForSeq2Seq, AdamW,
    get_scheduler
)
from accelerate import Accelerator
from huggingface_hub import Repository, get_full_repo_name
import warnings
warnings.filterwarnings("ignore")

# Key
model_checkpoint = "charliealex123/marian-finetuned-kde4-zh-to-en"
directory = "TMX"
train_file = "ZH-EN (Charlotte).json"
train_range = range(1_200, 2_000)
commit_text = "char_1200_2000"

# Constants
output_dir = f"{Project_path}/marian-finetuned-kde4-zh-to-en-local"
workdata_path = f"{workdata_path}/{directory}"
model_name = "marian-finetuned-kde4-zh-to-en"
repo_name = get_full_repo_name(model_name)

def preprocess_function(examples):
    max_input_length = 128
    max_target_length = 128
    inputs = [ex for ex in examples["zh"]]
    targets = [ex for ex in examples["en"]]
    model_inputs = tokenizer(inputs, max_length=max_input_length, truncation=True)

    # Set up the tokenizer for targets
    with tokenizer.as_target_tokenizer():
        labels = tokenizer(targets, max_length=max_target_length, truncation=True)

    model_inputs["labels"] = labels["input_ids"]
    return model_inputs

def compute_metrics(eval_preds):
    preds, labels = eval_preds
    # In case the model returns more than the prediction logits
    if isinstance(preds, tuple):
        preds = preds[0]

    decoded_preds = tokenizer.batch_decode(preds, skip_special_tokens=True)

    # Replace -100s in the labels as we can't decode them
    labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
    decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)

    # Some simple post-processing
    decoded_preds = [pred.strip() for pred in decoded_preds]
    decoded_labels = [[label.strip()] for label in decoded_labels]

    result = metric.compute(predictions=decoded_preds, references=decoded_labels)
    return {"bleu": result["score"]}

def postprocess(predictions, labels):
    predictions = predictions.cpu().numpy()
    labels = labels.cpu().numpy()

    decoded_preds = tokenizer.batch_decode(predictions, skip_special_tokens=True)

    # Replace -100 in the labels as we can't decode them.
    labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
    decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)

    # Some simple post-processing
    decoded_preds = [pred.strip() for pred in decoded_preds]
    decoded_labels = [[label.strip()] for label in decoded_labels]
    return decoded_preds, decoded_labels


# load data
os.chdir(workdata_path)
raw_datasets = load_dataset('json', data_files=train_file)
split_datasets = (raw_datasets['train']
    .select(train_range)
    .train_test_split(train_size=0.9, seed=20)
)
split_datasets["validation"] = split_datasets.pop("test")

# tokenize data
tokenizer = AutoTokenizer.from_pretrained(model_checkpoint, return_tensors="pt")
tokenized_datasets = split_datasets.map(
    preprocess_function,
    batched=True,
    remove_columns=split_datasets["train"].column_names,
)
tokenized_datasets.set_format("torch")

# model args
model = AutoModelForSeq2SeqLM.from_pretrained(model_checkpoint)
data_collator = DataCollatorForSeq2Seq(tokenizer, model=model)
metric = load_metric("sacrebleu")
args = Seq2SeqTrainingArguments(
    output_dir=model_name,
    evaluation_strategy="no",
    save_strategy="epoch",
    learning_rate=5e-5,
    per_device_train_batch_size=64,
    per_device_eval_batch_size=64,
    weight_decay=0.01,
    save_total_limit=3,
    num_train_epochs=3,
    predict_with_generate=True,
    fp16=False,
    push_to_hub=True,
)
train_dataloader = DataLoader(
    tokenized_datasets["train"],
    shuffle=True,
    collate_fn=data_collator,
    batch_size=8,
)
eval_dataloader = DataLoader(
    tokenized_datasets["validation"],
    collate_fn=data_collator,
    batch_size=8
)
optimizer = AdamW(model.parameters(), lr=2e-5)

# accelerator
accelerator = Accelerator()
model, optimizer, train_dataloader, eval_dataloader = accelerator.prepare(
    model, optimizer, train_dataloader, eval_dataloader
)
num_train_epochs = 3
num_update_steps_per_epoch = len(train_dataloader)
num_training_steps = num_train_epochs * num_update_steps_per_epoch

lr_scheduler = get_scheduler(
    "linear",
    optimizer=optimizer,
    num_warmup_steps=0,
    num_training_steps=num_training_steps,
)

# Start training
repo = Repository(output_dir, clone_from=repo_name)
progress_bar = tqdm(range(num_training_steps))
for epoch in range(num_train_epochs):
    # Training
    model.train()
    for batch in train_dataloader:
        outputs = model(**batch)
        loss = outputs.loss
        accelerator.backward(loss)

        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()
        progress_bar.update(1)

    # Evaluation
    model.eval()
    for batch in tqdm(eval_dataloader):
        with torch.no_grad():
            generated_tokens = accelerator.unwrap_model(model).generate(
                batch["input_ids"],
                attention_mask=batch["attention_mask"],
                max_length=128,
            )
        labels = batch["labels"]

        # Necessary to pad predictions and labels for being gathered
        generated_tokens = accelerator.pad_across_processes(
            generated_tokens, dim=1, pad_index=tokenizer.pad_token_id
        )
        labels = accelerator.pad_across_processes(labels, dim=1, pad_index=-100)

        predictions_gathered = accelerator.gather(generated_tokens)
        labels_gathered = accelerator.gather(labels)

        decoded_preds, decoded_labels = postprocess(predictions_gathered, labels_gathered)
        metric.add_batch(predictions=decoded_preds, references=decoded_labels)

    results = metric.compute()
    print(f"epoch {epoch}, BLEU score: {results['score']:.2f}")

    # Save and upload
    accelerator.wait_for_everyone()
    unwrapped_model = accelerator.unwrap_model(model)
    unwrapped_model.save_pretrained(output_dir, save_function=accelerator.save)
    if accelerator.is_main_process:
        tokenizer.save_pretrained(output_dir)
        repo.push_to_hub(
            commit_message=f"{commit_text}: Training in progress epoch {epoch}",
            blocking=False,
        )
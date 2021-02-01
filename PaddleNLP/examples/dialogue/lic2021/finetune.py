import os
import time
import math
import paddle
import paddle.distributed as dist
import paddle.nn as nn
import paddle.nn.functional as F
from paddle.io import DataLoader
from paddle.optimizer.lr import NoamDecay
from paddle.optimizer import AdamW

from args import parse_args, print_args
from model import BaselineModel
from data import DialogueDataset, Vocabulary


def load_ckpt(init_from_ckpt, model, optimizer=None):
    params_state_dict = paddle.load(init_from_ckpt + '.pdparams')
    model.set_state_dict(params_state_dict)
    if optimizer:
        opt_state_dict = paddle.load(init_from_ckpt + '.pdopt')
        optimizer.set_state_dict(opt_state_dict)
    print('Loaded checkpoint from %s' % init_from_ckpt)


def save_ckpt(model, optimizer, output_dir, name):
    params_path = os.path.join(output_dir, '{}.pdparams'.format(name))
    opt_path = os.path.join(output_dir, '{}.pdopt'.format(name))
    paddle.save(model.state_dict(), params_path)
    paddle.save(optimizer.state_dict(), opt_path)


def main(args):
    paddle.set_device('gpu' if args.n_gpus else 'cpu')
    world_size = dist.get_world_size()
    rank = dist.get_rank()
    if world_size > 1:
        dist.init_parallel_env()

    vocab = Vocabulary(args.vocab_file)

    model = BaselineModel(
        args.num_layers,
        args.d_model,
        args.nhead,
        args.dropout,
        args.activation,
        args.normalize_before,
        vocab.size,
        args.type_size,
        args.max_seq_len,
        args.min_dec_len,
        args.max_dec_len,
        args.topk,
        vocab.unk_id,
        vocab.bos_id,
        vocab.eos_id,
        vocab.mask_id,
        vocab.pad_id,
        is_infer=False)
    if world_size > 1:
        model = paddle.DataParallel(model)

    train_dataset = DialogueDataset(
        args.train_data_path,
        vocab,
        args.batch_size,
        args.sort_pool_size,
        args.seed,
        mode='train')
    train_dataloader = DataLoader(
        train_dataset, return_list=True, batch_size=None)
    valid_dataset = DialogueDataset(
        args.valid_data_path,
        vocab,
        args.batch_size,
        args.sort_pool_size,
        mode='valid')
    valid_dataloader = DataLoader(
        valid_dataset, return_list=True, batch_size=None)

    lr_scheduler = NoamDecay(1 / (args.warmup_steps * (args.lr**2)),
                             args.warmup_steps)
    optimizer = AdamW(
        learning_rate=lr_scheduler,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        grad_clip=nn.ClipGradByGlobalNorm(args.max_grad_norm))

    if args.init_from_ckpt:
        load_ckpt(args.init_from_ckpt, model)

    step = 0
    total_time = 0.0
    for epoch in range(args.epochs):
        if rank == 0:
            print('\nEpoch %d/%d' % (epoch + 1, args.epochs))
        batch_start_time = time.time()
        for inputs in train_dataloader:
            step += 1
            token_ids, type_ids, pos_ids, generation_mask, tgt_label, tgt_pos = inputs

            logits = model(
                (token_ids, type_ids, pos_ids, generation_mask, tgt_pos))
            loss = F.cross_entropy(logits, tgt_label)
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.clear_grad()

            total_time += (time.time() - batch_start_time)
            if rank == 0:
                if step % args.logging_steps == 0:
                    ppl = paddle.exp(loss)
                    print(
                        'step %d - loss: %.4f - ppl: %.4f - lr: %.7f - %.3fs/step'
                        % (step, loss, ppl, optimizer.get_lr(),
                           total_time / args.logging_steps))
                    total_time = 0.0
                if step % args.save_steps == 0:
                    evaluation(model, valid_dataloader)
                    save_ckpt(model, optimizer, args.save_dir, step)
            batch_start_time = time.time()


@paddle.no_grad()
def evaluation(model, data_loader):
    print('\nEval begin...')
    model.eval()
    total_tokens = 0
    total_loss = 0.0
    start_time = time.time()
    step = 0
    for inputs in data_loader:
        step += 1
        token_ids, type_ids, pos_ids, generation_mask, tgt_label, tgt_pos = inputs

        logits = model((token_ids, type_ids, pos_ids, generation_mask, tgt_pos))
        loss = F.cross_entropy(logits, tgt_label, reduction='sum')

        total_loss += loss.numpy()[0]
        total_tokens += tgt_label.shape[0]

    avg_loss = total_loss / total_tokens
    ppl = math.exp(avg_loss)
    avg_speed = (time.time() - start_time) / step
    print('loss: %.4f - ppl: %.4f - %.3fs/step\n' % (avg_loss, ppl, avg_speed))
    model.train()


if __name__ == '__main__':
    args = parse_args()
    print_args(args)

    if args.n_gpus > 1:
        dist.spawn(main, args=(args, ), nprocs=args.n_gpus)
    else:
        main(args)

import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from omegaconf import OmegaConf
from models.dit import DiT
from models import create_diffusion
from models.timestep_sampler import create_named_schedule_sampler
from ddad_utils.dataset import Dataset_maker
from ddad_utils.ddad import DDAD
from ddad_utils.metrics import Metric
import matplotlib.pyplot as plt
from tqdm import tqdm
from time import time
from datetime import timedelta
import pickle
import sys
import numpy as np
import warnings
warnings.filterwarnings('ignore')


def train(model, diffusion, sampler, dataloader, config):
    train_losses = []
    
    model.train()
    optimizer = optim.Adam(model.parameters(), lr=config.model.learning_rate)

    best_loss = float('inf')
    best_model_path = ""

    total_batches = config.model.epochs * len(dataloader)
    prev_time = time()

    for epoch in range(config.model.epochs):
        total_loss = 0

        for i, batch in enumerate(dataloader):
            if config.training.use_label:
                x, _ = batch  # label은 reconstruction에 사용하지 않음
            else:
                x = batch

            x = x.to(config.model.device)

            # diffusion timestep 샘플링
            t, _ = sampler.sample(x.shape[0], config.model.device)

            # 모델 학습
            loss_dict = diffusion.training_losses(model, x, t, model_kwargs={"y": x})
            loss = loss_dict["loss"].mean()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item()

            batches_done = epoch * len(dataloader) + i + 1
            batches_left = total_batches - batches_done
            time_left = timedelta(seconds=batches_left * (time() - prev_time))
            prev_time = time()

            sys.stdout.write(
            "\r[Epoch %d/%d] [Batch %d/%d] [Loss %f] [ETA: %s]" %
            (epoch+1,
            config.model.epochs,
            i+1,
            len(dataloader),
            loss.item(),
            str(time_left)[:-7])
        )

        avg_loss = total_loss / len(dataloader)
        train_losses.append(avg_loss)

        # print(f"\nEpoch {epoch+1}: Avg Loss = {avg_loss:.4f}\n")

        # 모델 저장
        if config.model.save_model:
            save_dir = os.path.join(config.model.checkpoint_dir, config.model.exp_name)
            os.makedirs(save_dir, exist_ok=True)
            # save_path = os.path.join(save_dir, f"{config.model.checkpoint_name}_epoch{epoch+1}.pt")
            # torch.save(model.state_dict(), save_path)
            # print(f"\n모델 저장됨: {save_path}")

            # 최적 모델 따로 저장
            if avg_loss < best_loss:
                best_loss = avg_loss
                best_model_path = os.path.join(save_dir, f"{config.model.checkpoint_name}_best.pt")
                torch.save(model.state_dict(), best_model_path)
                print(f"\n최고 성능 모델 갱신 (loss={best_loss:.4f}): {best_model_path}\n")

        
    train_results = {'train_loss':train_losses}
    with open(os.path.join(config.model.checkpoint_dir, config.model.exp_name, "train_losses.pickle"), 'wb') as f:
        pickle.dump(train_results,f)        
    print(f'Train results saved in {os.path.join(config.model.checkpoint_dir, config.model.exp_name)}.')


def main():
    # 1. config.yaml 불러오기
    config = OmegaConf.load("ddad_utils/config.yaml")
    os.makedirs(os.path.join(config.model.checkpoint_dir, config.model.exp_name), exist_ok=True)

    # 2. 디바이스 설정 및 시드 고정
    device = torch.device(config.model.device)
    torch.manual_seed(config.model.seed)

    # 3. 학습용 Dataset & DataLoader 구성
    train_dataset = Dataset_maker(
        root=config.data.data_dir,
        config=config,
        is_train=True,
        normalize=True
    )
    train_loader = DataLoader(
        train_dataset,
        batch_size=config.data.batch_size,
        shuffle=True,
        num_workers=config.model.num_workers,
    )

    # 4. DiT 모델 초기화
    model = DiT(
        input_size=config.data.seq_len,
        patch_size=5,
        in_channels=config.data.input_channel,
    ).to(device)

    # 5. pretrained weight 불러오기
    if config.model.load_chp > 0:
        load_path = os.path.join(config.model.checkpoint_dir, config.model.checkpoint_name + ".pt")
        if os.path.exists(load_path):
            print(f"Pretrained weight 로드 중: {load_path}")
            state_dict = torch.load(load_path, map_location=device)
            try:
                model.load_state_dict(state_dict, strict=True)
                print("weight 로딩 완료 (strict=True)")
            except RuntimeError as e:
                print("weight 로딩 실패 (구조 불일치):", e)
        else:
            print(f"weight 파일 없음: {load_path}")

    # 6. Diffusion & Sampler 구성
    diffusion = create_diffusion(
        timestep_respacing="1000",
        noise_schedule=config.model.noise_schedule,
        diffusion_steps=config.model.trajectory_steps,
    )
    sampler = create_named_schedule_sampler("uniform", diffusion)

    # 7. 학습 시작
    train(model, diffusion, sampler, train_loader, config)

    # Evaluation

    model = DiT(
        input_size=config.data.seq_len,
        patch_size=5,
        in_channels=config.data.input_channel,
    ).to(device)
    best_model_path = os.path.join(config.model.checkpoint_dir, config.model.exp_name, f"{config.model.checkpoint_name}_best.pt")
    model.load_state_dict(torch.load(best_model_path, map_location=device))
    model.eval()

    ddad = DDAD(model, config, normalize=True)
    predictions, labels_list, gt_list, forward_list, reconstructed_list = ddad()

    gt_array = np.array(gt_list)
    reconstructed_array = np.array(reconstructed_list)
    prediction_array = np.array(predictions)
    labels_array = np.array(labels_list)

    np.save('checkpoints/%s/gt_array.npy'%config.model.exp_name,gt_array)
    np.save('checkpoints/%s/reconstructed_array.npy'%config.model.exp_name,reconstructed_array)
    np.save('checkpoints/%s/prediction_array.npy'%config.model.exp_name,prediction_array)
    np.save('checkpoints/%s/labels_array.npy'%config.model.exp_name,labels_array)

    os.makedirs('checkpoints/%s/results_plot'%config.model.exp_name, exist_ok=True)

    for idx in tqdm(range(len(gt_array))):

        plt.figure(figsize=(10,6))

        plt.subplot(2,3,1)
        plt.plot(gt_array[idx][0])
        plt.title('Input signal / Thorax 1')

        plt.subplot(2,3,2)
        plt.plot(reconstructed_array[idx][0])
        plt.title('Reconstructed / Thorax 1')

        plt.subplot(2,3,3)
        plt.plot(prediction_array[idx*750:idx*750+749])
        plt.title('Input - Reconstructed')

        plt.subplot(2,3,4)
        plt.plot(gt_array[idx][1], color='darkorange')
        plt.title('Input signal / Thorax 2')

        plt.subplot(2,3,5)
        plt.plot(reconstructed_array[idx][1], color='darkorange')
        plt.title('Reconstructed / Thorax 2')

        plt.subplot(2,3,6)
        plt.plot(labels_array[idx*750:idx*750+749], color='darkorange')
        plt.title('Anomaly mask')

        plt.tight_layout()

        plt.savefig('checkpoints/%s/results_plot/data_%d.png'%(config.model.exp_name, idx+1))

        plt.close()

    metric = Metric(labels_array, prediction_array, config)
    metric.optimal_threshold()

    print("\n[Evaluation Metrics]")
    if config.metrics.auroc:
        print('AUROC: ({:.1f}, -)'.format(metric.image_auroc() * 100))
    if config.metrics.pro:
        print('PRO: {:.1f}'.format(metric.pixel_pro() * 100))
    if config.metrics.misclassifications:
        metric.miscalssified()
    metric.precision_recall_f1()



if __name__ == "__main__":
    main()

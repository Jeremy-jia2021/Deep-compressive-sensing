from torch.autograd import Variable
import scripts.operation as operation
import torch
import torch.nn as nn
from utils import util
from data import util as data_util
import numpy as np
import cv2
import random
from torchvision.utils import make_grid
import matplotlib.pyplot as plt
import torchvision.models as models
import spectral
from tensorboardX import SummaryWriter
from models.modules.encoder_decoder_arch import Quantizer
import cv2 as cv
import os
from PIL import Image
import math
import shutil
import copy
import scipy.io as io
import util as SPI_util
from SPI.data.data_loader import create_dataloader, create_dataset
from torch.nn.parallel import DataParallel, DistributedDataParallel
import models.networks as networks
import argparse
import options.options as option
import utils.util as util
from models import create_model
import utils.lr_scheduler as lr_scheduler
import utils.admm as optimizer
import pywt
from pytorch_wavelets import DTCWTForward, DTCWTInverse
import pandas as pd
import numpy as np
#import seaborn as sns


#%% options
TensorBoard_path = '../tb_logger/' + 'optimization'
pattern_path = './patterns'
test_img_path = './data/classic/lena.bmp'
illum_pattern = 'Hadamard'
SNR = 25

GT_size = 128
use_tensorboard = True
#%%

parser = argparse.ArgumentParser()
parser.add_argument('-opt', type=str, required=True, help='Path to options YMAL file.')
opt = option.parse(parser.parse_args().opt, is_train=False)
opt = option.dict_to_nonedict(opt)

class Solver(object):
    """Solver for training and testing StarGAN."""
    def __init__(self):
        """Initialize configurations."""
        # Miscellaneous.
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        #### create model
        model = create_model(opt)
        self.model = model.netG
        for param in self.model.parameters():
            param.requires_grad = False

        # Training configurations.
        self.batch_size = 1
        self.num_iters = 100
        self.num_iters_decay = 1000
        self.lr_update_step = 0.1
        self.lr = 0.005 # 0.0005 0.005
        self.beta1 = 0.5
        self.beta2 = 0.999
        # generate SPI masks
        self.Mask_generation()
        # get test images
        dataset = create_dataset(test_img_path)
        self.test_loader = create_dataloader(dataset) if dataset.test_dataset is not None else None

        self.J = 5
        self.xfm = DTCWTForward(J=self.J, biort='near_sym_b', qshift='qshift_b').to(self.device)
        self.ifm = DTCWTInverse(biort='near_sym_b', qshift='qshift_b').to(self.device)

        # Build the model and tensorboard.
        self.cri_l2 = nn.MSELoss().to(self.device)
        self.cri_tv = SPI_util.TVLoss().to(self.device)
        self.cri_l1 = nn.L1Loss().to(self.device)
        self.cri_sl1 = nn.SmoothL1Loss().to(self.device)
        self.Net_VGG = SPI_util.Net_VGG().to(self.device)
        if use_tensorboard:
            self.build_tensorboard()

        # load Quantizer defined in the original encoder_decorder network
        self.Quantizer = Quantizer().to(self.device)

    def Mask_generation(self, size=GT_size, is_update=True):
        if is_update:
            self.SPI_mask, self.SPI_index, _ = SPI_util.get_pattern(illum_pattern, size)
            io.savemat(pattern_path + '/SPImask.mat', {'Pattern': self.SPI_mask})
        else:
            _, self.SPI_index = SPI_util.get_pattern(illum_pattern, size)
            tmp = io.loadmat(pattern_path + '/SPImask.mat')
            self.SPI_mask = tmp['Pattern']
        self.SPI_mask = torch.from_numpy(self.SPI_mask[np.newaxis,:, :, :]).float().to(self.device)

    def setup_optimizer(self, initial_rec):
        # Yl, Yh = self.xfm(initial_rec.to(self.device))
        # self.Y = []
        # self.Y.append(Variable(Yl, requires_grad=True))
        # for i in range(self.J):
        #     self.Y.append(Variable(Yh[i], requires_grad=True))

        self.Y = Variable(initial_rec.clone(), requires_grad=True)

        # randn
        self.optimizer = torch.optim.Adam([self.Y], self.lr, [self.beta1, self.beta2])
        self.scheduler = lr_scheduler.MultiStepLR_Restart(self.optimizer, [500, 1000, 1500],
                                                     restarts=[],
                                                     weights=[],
                                                     gamma=0.5,
                                                     clear_state=[])  # 200, 400, 600, 1000, 1500
    def encoder_output(self, input_img):
        # input_img = SPI_util.norm(input_img)
        return self.model(input=input_img, is_output_z=True)

    def encoder_decoder_output(self, input_fea):
        input_fea = SPI_util.norm(input_fea)
        output = self.model(input=input_fea, is_bottleneck_fea=True)
        rec_z = self.model(input=output, is_output_z=True)
        return output, rec_z

    def build_tensorboard(self):
        """Build a tensorboard logger."""
        self.tb_logger = SummaryWriter(log_dir=TensorBoard_path)

    def update_lr(self, lr):
        """Decay learning rates of the generator and discriminator."""
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr

    def reset_grad(self):
        """Reset the gradient buffers."""
        self.optimizer.zero_grad()

    def train(self):
        f_conv_psnr = []
        f_deep_psnr = []
        f_conv_ssim = []
        f_deep_ssim = []
        f_conv_mse  = []
        f_deep_mse  = []
        for loops in [5]: # [5, 17, 9]
            if self.test_loader is not None:
                data_iter = iter(self.test_loader)
                for i in range(0+loops):
                    test_data = next(data_iter).to(self.device)
            else:
                test_data = SPI_util.load_images(test_img_path, GT_size).to(self.device)

            idea_meas = SPI_util.get_sim_meas(test_data, self.SPI_mask)
            io.savemat(pattern_path + '/idea_meas.mat', {'idea_meas': idea_meas.cpu().numpy()})
            # plt.plot(idea_meas.squeeze().cpu())
            # plt.show()

            # idea_meas = idea_meas - torch.mean(idea_meas)
            # idea_meas = torch.log(idea_meas/torch.mean(idea_meas))
            # noise = util.add_Gaussian_noise(idea_meas, SNR=SNR)*0.02
            noise = 1e1*torch.normal(mean=0, std=torch.tensor(.5).expand_as(idea_meas)).cuda()
            meas = idea_meas + noise
            self.test_measurement = SPI_util.diff_meas(meas) if illum_pattern is not "Fourier" else meas
            conv_rec = SPI_util.get_conv_rec(self.test_measurement, self.SPI_mask, self.SPI_index).to(self.device)
            self.setup_optimizer(conv_rec)

            print('Start iterating... {: d}'.format(loops+1))
            for iteration in range(1500): #2500
                # =================================================================================== #
                #                                      2. Training                                    #
                # =================================================================================== #
                # Decay learning rates.
                self.scheduler.step()

               # output = self.ifm((self.Y[0], self.Y[1:len(self.Y)]))
                rec_measurement = SPI_util.get_sim_meas(self.Y, self.SPI_mask)#* 1.4# self.k
                rec_measurement = SPI_util.diff_meas(rec_measurement) if illum_pattern is not "Fourier" else rec_measurement

                # loss calculation
                loss = 1 * self.cri_l2(rec_measurement, self.test_measurement) # 2
                loss += 0.01 * self.cri_tv(self.Y)  # 1

                Yl, Yh = self.xfm(self.Y)
                loss += 0.5 * torch.norm(Yl, p=1)
                for i in range(len(Yh)):
                    loss += 0.5*(6-i) * torch.norm(Yh[i], p=1)

                self.reset_grad()
                loss.backward()
                self.optimizer.step()

                # =================================================================================== #
                #                                     3. Validation                                   #
                # =================================================================================== #
                output = self.Y
                if iteration % 50 == 0:
                    # normalize images
                    output = SPI_util.normalize_0_to_1(output.detach())
                    test_data = SPI_util.normalize_0_to_1(test_data)
                    conv_rec = SPI_util.normalize_0_to_1(conv_rec)
                    SR_cube = output.detach().cpu().numpy()
                    GT_data = test_data.detach().cpu().numpy()
                    CONV_REC = conv_rec.cpu().numpy()

                    # Calculate PSNR
                    conv_psnr = util.calculate_psnr(CONV_REC * 256, GT_data * 256)
                    deep_psnr = util.calculate_psnr(SR_cube * 256, GT_data * 256)
                    conv_ssim = util.calculate_ssim(CONV_REC * 256, GT_data * 256)
                    deep_ssim = util.calculate_ssim(SR_cube * 256, GT_data * 256)
                    conv_mse = util.calculate_mse(CONV_REC * 256, GT_data * 256)
                    deep_mse = util.calculate_mse(SR_cube * 256, GT_data * 256)

                    self.tb_logger.add_scalar('Conv/PSNR', conv_psnr, iteration)
                    self.tb_logger.add_scalar('Conv/SSIM', conv_ssim, iteration)
                    self.tb_logger.add_scalar('Conv/MSE', conv_mse, iteration)
                    self.tb_logger.add_scalar('Deep/PSNR', deep_psnr, iteration)
                    self.tb_logger.add_scalar('Deep/SSIM', deep_ssim, iteration)
                    self.tb_logger.add_scalar('Deep/MSE', deep_mse, iteration)
                    self.tb_logger.add_scalar('loss/loss', loss, iteration)

                    output_grid = make_grid(output[0:6, ...], normalize=True)
                    test_data_grid = make_grid(test_data[0:6, ...], normalize=True)
                    conv_rec_grid = make_grid(conv_rec[0:6, ...], normalize=True)

                    self.tb_logger.add_image('Res/Learned', output_grid, global_step=iteration, dataformats='CHW')
                    self.tb_logger.add_image('Res/GT', test_data_grid, global_step=iteration, dataformats='CHW')
                    self.tb_logger.add_image('Res/Conv.', conv_rec_grid, global_step=iteration, dataformats='CHW')
                    #self.tb_logger.add_image('Res/bottleneck_fea_img', self.z.squeeze(), global_step=iteration, dataformats='CHW')
                    print('iteration = ', iteration)
                  #  print('output_max = {:.3f}, k1 = {:.6f}'.format(torch.max(output[0,0,...]), self.k))

            f_conv_psnr.append(conv_psnr)
            f_deep_psnr.append(deep_psnr)
            f_conv_ssim.append(conv_ssim)
            f_deep_ssim.append(deep_ssim)
            f_conv_mse.append(conv_mse)
            f_deep_mse.append(deep_mse)

        f_conv_psnr = np.mean(np.array(f_conv_psnr))
        f_deep_psnr = np.mean(np.array(f_deep_psnr))
        f_conv_ssim = np.mean(np.array(f_conv_ssim))
        f_deep_ssim = np.mean(np.array(f_deep_ssim))
        f_conv_mse = np.mean(np.array(f_conv_mse))
        f_deep_mse = np.mean(np.array(f_deep_mse))

        # rmse_percent = (-f_deep_mse + f_conv_mse) / f_conv_mse * 100
        # psnr_percent = (f_deep_psnr - f_conv_psnr) / f_conv_psnr * 100
        # ssim_percent = (f_deep_ssim - f_conv_ssim) / f_conv_ssim * 100

        # print('rmse_mean = {:.2f}, '.format(np.mean(rmse_percent)))
        # print('rmse_std = {:.2f}, '.format(np.std(rmse_percent)))
        # print('psnr_mean = {:.2f}, '.format(np.mean(psnr_percent)))
        # print('psnr_std = {:.2f}, '.format(np.std(psnr_percent)))
        # print('ssim_mean = {:.2f}, '.format(np.mean(ssim_percent)))
        # print('ssim_std = {:.2f}, '.format(np.std(ssim_percent)))


        print('f_conv_mse = {:.1f}, '.format(f_conv_mse))
        print('f_deep_mse = {:.1f}, '.format(f_deep_mse))
        print('f_conv_psnr = {:.2f}, '.format(f_conv_psnr))
        print('f_deep_psnr = {:.2f}, '.format(f_deep_psnr))
        print('f_conv_ssim = {:.4f}, '.format(f_conv_ssim))
        print('f_deep_ssim = {:.4f}, '.format(f_deep_ssim))



def main():
    filelist = [f for f in os.listdir(pattern_path) if f.endswith(".jpg")]
    for f in filelist:
        os.remove(os.path.join(pattern_path, f))
    #os.system('rm -r /data/jia/Networks/single-pixel/CS-GAN-test1/tb_logger/optimization')
    os.system('rm -r /data/jia/Networks/single-pixel/CS-GAN-test1/tb_logger/optimization')

    my_whole_seed = 222
    random.seed(my_whole_seed)
    np.random.seed(my_whole_seed)
    torch.manual_seed(my_whole_seed)
    torch.cuda.manual_seed_all(my_whole_seed)
    torch.cuda.manual_seed(my_whole_seed)
    np.random.seed(my_whole_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    os.environ['PYTHONHASHSEED'] = str(my_whole_seed)

    solver = Solver()
    solver.train()

if __name__ == '__main__':
    main()
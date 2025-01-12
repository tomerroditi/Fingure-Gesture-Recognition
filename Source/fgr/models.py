import copy
import time
import random
import numpy as np
import sklearn.preprocessing
import torch.optim
import matplotlib.pyplot as plt
import torch.nn as nn
import torch
import torch.nn.functional as functional

from torch.utils.data import TensorDataset, DataLoader
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau
from tqdm import tqdm
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import StratifiedKFold
from datetime import datetime
from collections import Counter

from .data_manager import Real_Time_Recording
from .pipelines import Data_Pipeline
from ..streamer.data import Data, ConnectionTimeoutError


##############################################
# Predictors
##############################################
class Real_Time_Predictor:
    def __init__(self, model, data_stream: Data, pipeline: Data_Pipeline, vote_over: int = 10, max_timeout: float = 15):
        self.recording = Real_Time_Recording(data_stream, pipeline)
        self.model = model
        self.vote_over = vote_over
        self.predictions_stack = []

        # Confirm initial data retrieval before continuing (or raise Error if timeout)
        init_time = datetime.now()
        while not (data_stream.is_connected and data_stream.has_data):
            time.sleep(0.1)
            if (datetime.now() - init_time).seconds > max_timeout:
                if not data_stream.is_connected:
                    raise ConnectionTimeoutError
                elif not data_stream.has_data:
                    raise TimeoutError(f"Did not succeed to stream data within {max_timeout} seconds.")

    def majority_vote_predict(self) -> (str, float):
        feats = self.recording.get_feats_for_prediction()
        pred = self.model.classify(feats)
        self.predictions_stack.append(pred[0])
        if len(self.predictions_stack) > self.vote_over:
            self.predictions_stack.pop(0)
        if len(self.predictions_stack) < self.vote_over:
            return 'loading predictions...', 1

        counter = Counter(self.predictions_stack)
        majority = counter.most_common(1)[0][0]
        confidence = counter[majority] / len(self.predictions_stack)
        return majority, confidence


##############################################
# Models
##############################################

class simple_CNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.label_encoder = LabelEncoder()
        self.accu_vals = {}
        self.loss_vals = {}
        self.train_data = np.empty(0)
        self.train_labels = np.empty(0)
        self.val_data = np.empty(0)
        self.val_labels = np.empty(0)

    def cv_fit_model(self, data: np.array, labels: np.array, num_folds: int = 5, batch_size: int = 64,
                     lr: float = 0.001, l2_weight: float = 0.0001, num_epochs: int = 200, plot_cm: bool = False) ->\
            (list[nn.Module], list[float]):
        """
        This function performs cross validation training on the model
        inputs:
            data: numpy array of data
            labels: numpy array of labels
            num_folds: int, the number of folds to split the data to
            batch_size: int, the size of the batches to be used in the data loaders
            lr: float, the learning rate to be used in the optimizer
            l2_weight: float, the weight of the l2 regularization
            num_epochs: int, the number of epochs to train the model
        outputs:
            models: list of models, each model is a copy of the original model fitted on a different fold
            accu_vals: list of floats, the accuracy of each model on its validation fold
        """
        fold_idx = self.folds_split_by_gesture(labels, num_folds)
        # fit the label encoder
        self.label_encoder.fit(np.char.strip(labels, '_0123456789'))
        # cv training loop
        models = []
        accu_vals = []
        for fold in np.unique(fold_idx):
            curr_model = copy.deepcopy(self)  # create a copy of the model for each fold
            # split the data to train and test of the current fold
            train_idx = fold_idx != fold
            val_idx = fold_idx == fold
            curr_model.fit_model(data[train_idx], labels[train_idx], data[val_idx], labels[val_idx], batch_size, lr,
                                 l2_weight, num_epochs)
            models.append(curr_model)
            accu_vals.append(curr_model.evaluate_model(data[val_idx], labels[val_idx], plot_cm=plot_cm))

        return models, accu_vals

    @staticmethod
    def folds_split_by_gesture(labels: np.array = None, num_folds: int = 5, seed: int = 42) -> np.array:
        """
        split data into k folds, each fold will contain data from different gestures repetitions, e.g. if we have
        10 repetitions of "fist" gestures from an experiment ('001_1_1_fist_1', '001_1_1_fist_2', ..., '001_1_1_fist_10')
        and we want 5 folds than each fold will contain 2 repetitions of "fist" gestures.
        inputs:
            labels: numpy array of labels, each label should be a string
            num_folds: int, the number of folds to split the data to
        outputs:
            fold_idx: a numpy array with the fold number for each sample
        """
        unique_labels = np.unique(labels)
        unique_labels_no_num = np.char.strip(unique_labels, '_0123456789')

        # in case there is a gesture with less than num_folds repetition we will discard it since we cant split it in a
        # stratified way.
        too_less_rep = np.array([np.sum(unique_labels_no_num == label) < num_folds for label in unique_labels_no_num])
        print(f'number of gestures with less than {num_folds} repetition: {np.sum(too_less_rep)}\n'
              f'Their names are: {unique_labels_no_num[too_less_rep]}'
              f'These gestures have been discarded from the dataset')
        unique_labels = unique_labels[np.logical_not(too_less_rep)]
        unique_labels_no_num = unique_labels_no_num[np.logical_not(too_less_rep)]

        # split the unique labels (subjects gestures with repetition) to folds according to the gesture name (without
        # the repetition and subject number)
        skf = StratifiedKFold(num_folds, random_state=seed, shuffle=True)
        fold_idx_unique_labels = np.zeros(unique_labels.shape)
        for i, (_, test_idx) in enumerate(skf.split(unique_labels, unique_labels_no_num)):
            fold_idx_unique_labels[test_idx] = i
        fold_idx_unique_labels = np.array(fold_idx_unique_labels, dtype=int)

        # now split the data itself to folds
        fold_idx = np.zeros(labels.shape)
        for i, label in zip(fold_idx_unique_labels, unique_labels):
            fold_idx[labels == label] = i

        return fold_idx

    def fit_model(self, train_data: np.array, train_labels: np.array, val_data: np.array = None,
                  val_labels: np.array = None, batch_size: int = 64, lr=0.001,
                  l2_weight: float = 0.0001, num_epochs: int = 200) -> None:
        """This function trains a model and returns the train and test loss and accuracy"""
        if val_data is not None and val_labels is None:
            raise ValueError('If test data is provided, test labels must be provided as well')

        # save the data and labels for later use
        self.train_data = train_data
        self.train_labels = train_labels
        self.val_data = val_data
        self.val_labels = val_labels

        # strip the labels from the numbers - the model will predict the gesture name and not the repetition number or
        # subject number (the numbers are used for data splitting)
        train_labels = np.char.strip(train_labels, '_0123456789')
        train_labels = self.label_encoder.fit_transform(train_labels)
        # create data loaders
        train_dl = self.create_dataloader(train_data, train_labels, batch_size, drop_last=True)

        val_dl = None
        if val_labels is not None:
            val_labels = np.char.strip(val_labels, '_0123456789')
            val_labels = self.label_encoder.transform(val_labels)
            val_dl = self.create_dataloader(val_data, val_labels, batch_size, drop_last=False)

        # train the model
        optimizer = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=l2_weight)
        loss_func = nn.CrossEntropyLoss()
        # move the model to the gpu if available
        if torch.cuda.is_available():
            self.cuda()

        self.train_model(train_dl, val_dl, num_epochs, optimizer, loss_func)

    @staticmethod
    def create_dataloader(data: np.array, labels: np.array, batch_size: int = 64, drop_last: bool = True) -> (
            DataLoader, DataLoader):
        """
        This function created data loaders from numpy arrays of data and targets (train and test)
        inputs:
            data: numpy array of data
            labels: numpy array of labels (should be integers)
            batch_size: int, the size of the batches to be used in the data loaders
            drop_last: bool, if True the last batch will be dropped if it is smaller than the batch size
        outputs:
            data_loader: DataLoader object, contains the training data
        """
        # convert the data to torch tensors, and move them to the GPU if available
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        data = torch.from_numpy(data).float().to(device)
        labels = torch.from_numpy(labels).long().to(device)
        # create the data loader
        dataset_torch = TensorDataset(data, labels)
        dataloader = DataLoader(dataset_torch, batch_size=batch_size, shuffle=True, drop_last=drop_last)

        return dataloader

    def train_model(self, train_dataloader: DataLoader, val_dataloader: DataLoader, num_epochs: int,
                    optimizer, loss_function):
        # TODO: add type hints for optimizer and loss_function
        # initialize the variables for the loss and accuracy plotting
        self.loss_vals = {'train': [], 'val': []}  # loss history
        self.accu_vals = {'train': [], 'val': []}
        x_epoch = range(num_epochs)
        scheduler = StepLR(optimizer, step_size=100, gamma=0.1)

        for _ in tqdm(range(num_epochs), desc='training model', unit='epoch'):
            self.float()
            self.train()
            self._train_loop(train_dataloader, loss_function, optimizer)
            self.eval()
            self._val_loop(val_dataloader, loss_function)
            scheduler.step()

        # create a plot for the loss and accuracy in the training process
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5))  # subplot for loss and accuracy
        ax1.set_title('Loss'), ax1.set_xlabel('Epoch'), ax1.set_ylabel('Loss')
        ax2.set_title('Accuracy'), ax2.set_xlabel('Epoch'), ax2.set_ylabel('Accuracy')
        # train graph
        ax1.plot(x_epoch, self.loss_vals['train'], label='train')
        ax2.plot(x_epoch, self.accu_vals['train'], label='train')
        # validation graph
        if val_dataloader is not None:
            ax1.plot(x_epoch, self.loss_vals['val'], label='val')
            ax2.plot(x_epoch, self.accu_vals['val'], label='val')
        ax1.legend(), ax2.legend()
        plt.show()

        print("Done Training!")

    def _train_loop(self, dataloader, loss_fn, optimizer) -> None:
        for batch, (X, y) in enumerate(dataloader):
            # Compute prediction and loss
            pred = self(X.float())
            loss = loss_fn(pred, y.long())

            # Backpropagation
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        train_loss, train_accu = self._calc_loss_accu(dataloader, loss_fn)
        self.loss_vals['train'].append(train_loss)  # average loss per batch
        self.accu_vals['train'].append(train_accu * 100)  # accuracy in percent

    def _val_loop(self, dataloader, loss_fn) -> None:
        if dataloader is None:
            return
        val_loss, val_accu = self._calc_loss_accu(dataloader, loss_fn)
        self.loss_vals['val'].append(val_loss)  # average loss per batch
        self.accu_vals['val'].append(val_accu * 100)  # accuracy in percent

    def _calc_loss_accu(self, dataloader, loss_fn) -> (float, float):
        loss, correct = 0, 0
        with torch.no_grad():
            for X, y in dataloader:
                pred = self(X.float())
                loss += loss_fn(pred, y.long()).item()
                correct += (pred.argmax(1) == y).type(torch.float).sum().item()  # noqa
        accu = correct / len(dataloader.dataset)
        loss = loss / len(dataloader)
        return loss, accu

    def evaluate_model(self, data: np.array, labels: np.array, plot_cm: bool = True, cm_title: str = 'untitled') -> \
            float:
        predictions = self.classify(data)  # get the gesture of each sample
        unique_labels = np.unique(labels)
        unique_labels_pred = []
        for label in unique_labels:
            curr_predictions = predictions[labels == label]  # get the predictions of the current label
            # majority voting - get a single prediction per gesture
            unique, pos = np.unique(curr_predictions, return_inverse=True)  # Finds unique elements and their positions
            pred = unique[np.bincount(pos).argmax()]
            unique_labels_pred.append(pred)

        # strip the gesture number and experiment name from the true unique labels
        unique_labels_striped = np.char.strip(unique_labels, '_0123456789')

        # calculate the accuracy
        accuracy = np.sum(unique_labels_pred == unique_labels_striped) / len(unique_labels_striped)

        if plot_cm:
            plt.figure()
            # create and display a confusion matrix
            tick_labels = np.sort(np.unique(unique_labels_striped))  # use it to make sure that the display is aligned
            # with cm order
            cm = sklearn.metrics.confusion_matrix(unique_labels_striped, unique_labels_pred, labels=tick_labels)
            cm_disp = sklearn.metrics.ConfusionMatrixDisplay(cm, display_labels=tick_labels)
            cm_disp.plot(cmap='Blues', xticks_rotation=45)
            cm_disp.ax_.set_title(f'{cm_title} - Accuracy: {accuracy:.3f}')
            plt.show()
        return accuracy

    def classify(self, data: np.array) -> np.array:
        """classify the data according to the model's label encoder"""
        # convert the data to a tensor and allocate to the same device as the model
        device = next(self.parameters()).device
        data = torch.from_numpy(data).float().to(device)
        # forward pass - get the probabilities of each class
        scores = self(data)
        # get the class with the highest probability
        predictions = torch.argmax(scores, dim=1)
        predictions = predictions.cpu().detach().numpy()
        # convert the class number to the gesture name
        predictions = self.label_encoder.inverse_transform(predictions)
        return predictions


class Net(simple_CNN):
    def __init__(self, num_classes: int = 10, dropout_rate: float = 0.3):
        super().__init__()
        self.dropout_rate = dropout_rate

        self.conv_1 = nn.Conv2d(1, 8, 2, padding='same')
        self.batch_norm_1 = nn.BatchNorm2d(8)
        self.conv_2 = nn.Conv2d(8, 16, 2, padding='same')
        self.batch_norm_2 = nn.BatchNorm2d(16)
        self.fc_1 = nn.Linear(4*4*16, 40)  # 4*4 from image dimension, 4 from num of filters
        self.batch_norm_3 = nn.BatchNorm1d(40)
        self.fc_2 = nn.Linear(40, 20)
        self.batch_norm_4 = nn.BatchNorm1d(20)
        self.fc_3 = nn.Linear(20, num_classes)

    def forward(self, x):
        # add white gaussian noise to the input only during training
        # if self.training:
        #     noise_percentage = 0.2
        #     random_idx = torch.rand(x.shape[0]) < noise_percentage
        #     if torch.sum(random_idx) > 0:
        #         x[random_idx] = x[random_idx] + torch.randn(x[random_idx].shape) * 0.03 * (
        #                     torch.max(x[random_idx], dim=0)[0] - torch.min(x[random_idx], dim=0)[0])
        #         noise = torch.randn(x.shape) * 0.05 * (float(torch.max(x)) - float(torch.min(x)))  # up to 5% noise
        #         noise = noise.to(x.device)
        #         x = x + noise
        x = self.conv_1(x)
        x = self.batch_norm_1(x)
        x = functional.relu(x)
        x = self.conv_2(x)
        x = self.batch_norm_2(x)
        x = functional.relu(x)
        x = torch.flatten(x, 1)
        x = self.fc_1(x)
        x = self.batch_norm_3(x)
        x = functional.relu(x)
        x = functional.dropout(x, p=self.dropout_rate, training=self.training)
        x = self.fc_2(x)
        x = self.batch_norm_4(x)
        x = functional.relu(x)
        x = functional.dropout(x, p=self.dropout_rate, training=self.training)
        x = self.fc_3(x)
        x = functional.softmax(x, dim=1)
        return x

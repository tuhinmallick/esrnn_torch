import os
import time

import numpy as np
import pandas as pd

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR

from pathlib import Path
from copy import deepcopy

from ESRNN.utils.config import ModelConfig
from ESRNN.utils.ESRNN import _ESRNN
from ESRNN.utils.losses import SmylLoss, PinballLoss
from ESRNN.utils.data import Iterator

from ESRNN.utils_evaluation import owa


class ESRNN(object):
  """ Exponential Smoothing Recurrent Neural Network

  Pytorch Implementation of the M4 time series forecasting competition winner.
  Proposed by Smyl. The model uses a hybrid approach of Machine Learning and
  statistical methods by combining recurrent neural networks to model a common
  trend with shared parameters across series, and multiplicative Holt-Winter
  exponential smoothing.

  Parameters
  ----------
  max_epochs: int
    maximum number of complete passes to train data during fit
  freq_of_test: int
    period for the diagnostic evaluation of the model.
  learning_rate: float
    size of the stochastic gradient descent steps
  lr_scheduler_step_size: int
    this step_size is the period for each learning rate decay
  per_series_lr_multip: float
    multiplier for per-series parameters smoothing and initial
    seasonalities learning rate (default 1.0)
  gradient_eps: float
    term added to the Adam optimizer denominator to improve
    numerical stability (default: 1e-8)
  gradient_clipping_threshold: float
    max norm of gradient vector, with all parameters treated
    as a single vector
  rnn_weight_decay: float
    parameter to control classic L2/Tikhonov regularization
    of the rnn parameters
  noise_std: float
    standard deviation of white noise added to input during
    fit to avoid the model from memorizing the train data
  level_variability_penalty: float
    this parameter controls the strength of the penalization
    to the wigglines of the level vector, induces smoothness
    in the output
  testing_percentile: float
    This value is only for diagnostic evaluation.
    In case of percentile predictions this parameter controls
    for the value predicted, when forecasting point value,
    the forecast is the median, so percentile=50.
  training_percentile: float
    To reduce the model's tendency to over estimate, the
    training_percentile can be set to fit a smaller value
    through the Pinball Loss.
  batch_size: int
    number of training examples for the stochastic gradient steps
  seasonality: int list
    list of seasonalities of the time series
    Hourly [24, 168], Daily [7], Weekly [52], Monthly [12],
    Quarterly [4], Yearly [].
  input_size: int
    input size of the recurrent neural network, usually a
    multiple of seasonality
  output_size: int
    output_size or forecast horizon of the recurrent neural
    network, usually multiple of seasonality
  random_seed: int
    random_seed for pseudo random pytorch initializer and
    numpy random generator
  exogenous_size: int
    size of one hot encoded categorical variable, invariannt
    per time series of the panel
  min_inp_seq_length: int
    description
  max_periods: int
    Parameter to chop longer series, to last max_periods,
    max e.g. 40 years
  cell_type: str
    Type of RNN cell, available GRU, LSTM, RNN, ResidualLSTM.
  state_hsize: int
    dimension of hidden state of the recurrent neural network
  dilations: int list
    each list represents one chunk of Dilated LSTMS, connected in
    standard ResNet fashion
  add_nl_layer: bool
    whether to insert a tanh() layer between the RNN stack and the
    linear adaptor (output) layers
  device: str
    pytorch device either 'cpu' or 'cuda'
  Notes
  -----
  **References:**
  `M4 Competition Conclusions
  <https://rpubs.com/fotpetr/m4competition>`__
  `Original Dynet Implementation of ESRNN
  <https://github.com/M4Competition/M4-methods/tree/master/118%20-%20slaweks17>`__
  """
  def __init__(self, max_epochs=15, batch_size=1, batch_size_test=64, freq_of_test=-1,
               learning_rate=1e-3, lr_scheduler_step_size=9, lr_decay=0.9,
               per_series_lr_multip=1.0, gradient_eps=1e-8, gradient_clipping_threshold=20,
               rnn_weight_decay=0, noise_std=0.001,
               level_variability_penalty=80,
               testing_percentile=50, training_percentile=50, ensemble=False,
               cell_type='LSTM',
               state_hsize=40, dilations=[[1, 2], [4, 8]],
               add_nl_layer=False, seasonality=[4], input_size=4, output_size=8,
               frequency=None, max_periods=20, random_seed=1,
               device='cpu', root_dir='./'):
    super(ESRNN, self).__init__()
    self.mc = ModelConfig(max_epochs=max_epochs, batch_size=batch_size, batch_size_test=batch_size_test,
                          freq_of_test=freq_of_test, learning_rate=learning_rate,
                          lr_scheduler_step_size=lr_scheduler_step_size, lr_decay=lr_decay,
                          per_series_lr_multip=per_series_lr_multip,
                          gradient_eps=gradient_eps, gradient_clipping_threshold=gradient_clipping_threshold,
                          rnn_weight_decay=rnn_weight_decay, noise_std=noise_std,
                          level_variability_penalty=level_variability_penalty,
                          testing_percentile=testing_percentile, training_percentile=training_percentile,
                          ensemble=ensemble,
                          cell_type=cell_type,
                          state_hsize=state_hsize, dilations=dilations, add_nl_layer=add_nl_layer,
                          seasonality=seasonality, input_size=input_size, output_size=output_size,
                          frequency=frequency, max_periods=max_periods, random_seed=random_seed,
                          device=device, root_dir=root_dir)
    self._fitted = False

  def train(self, dataloader, max_epochs,
            warm_start=False, shuffle=True, verbose=True):
    """
    Auxiliary function, pytorch train procedure for the ESRNN model

    Parameters:
    -------
    dataloader: pytorch dataloader
    max_epochs: int
    warm_start: bool
    shuffle: bool
    verbose: bool
    
    Returns
    -------
    self : returns an instance of self.
    """

    if self.mc.ensemble:
      self.esrnn_ensemble = [deepcopy(self.esrnn).to(self.mc.device)] * 5

    if verbose: print(15*'='+' Training ESRNN  ' + 15*'=' + '\n')

    # Optimizers
    if not warm_start:
      self.es_optimizer = optim.Adam(params=self.esrnn.es.parameters(),
                                lr=self.mc.learning_rate*self.mc.per_series_lr_multip,
                                betas=(0.9, 0.999), eps=self.mc.gradient_eps)

      self.es_scheduler = StepLR(optimizer=self.es_optimizer,
                            step_size=self.mc.lr_scheduler_step_size,
                            gamma=0.9)

      self.rnn_optimizer = optim.Adam(params=self.esrnn.rnn.parameters(),
                                lr=self.mc.learning_rate,
                                betas=(0.9, 0.999), eps=self.mc.gradient_eps,
                                weight_decay=self.mc.rnn_weight_decay)

      self.rnn_scheduler = StepLR(optimizer=self.rnn_optimizer,
                            step_size=self.mc.lr_scheduler_step_size,
                            gamma=self.mc.lr_decay)

    # Loss Functions
    train_tau = self.mc.training_percentile / 100
    train_loss = SmylLoss(tau=train_tau,
                          level_variability_penalty=self.mc.level_variability_penalty)

    eval_tau = self.mc.testing_percentile / 100
    eval_loss = PinballLoss(tau=eval_tau)

    for epoch in range(max_epochs):
      self.esrnn.train()
      start = time.time()
      if shuffle:
        dataloader.shuffle_dataset(random_seed=epoch)
      losses = []
      for _ in range(dataloader.n_batches):
        self.es_optimizer.zero_grad()
        self.rnn_optimizer.zero_grad()

        batch = dataloader.get_batch()
        windows_y, windows_y_hat, levels = self.esrnn(batch)

        # Pinball loss on normalized values
        loss = train_loss(windows_y, windows_y_hat, levels)
        losses.append(loss.data.cpu().numpy())
        #print("loss", loss)

        loss.backward()

        torch.nn.utils.clip_grad_norm_(self.esrnn.rnn.parameters(),
                                       self.mc.gradient_clipping_threshold)
        torch.nn.utils.clip_grad_norm_(self.esrnn.es.parameters(),
                                       self.mc.gradient_clipping_threshold)
        self.rnn_optimizer.step()
        self.es_optimizer.step()

      # Decay learning rate
      self.es_scheduler.step()
      self.rnn_scheduler.step()

      if self.mc.ensemble:
        copy_esrnn = deepcopy(self.esrnn)
        copy_esrnn.eval()
        self.esrnn_ensemble.pop(0)
        self.esrnn_ensemble.append(copy_esrnn)


      # Evaluation
      self.train_loss = np.mean(losses)
      if verbose:
        print(f"========= Epoch {epoch} finished =========")
        print(f"Training time: {round(time.time() - start, 5)}")
        print("Training loss ({} prc): {:.5f}".format(self.mc.training_percentile,
                                                      self.train_loss))

      if (epoch % self.mc.freq_of_test == 0) and (self.mc.freq_of_test > 0):
        if self.y_test_df is not None:
          self.test_loss = self.model_evaluation(dataloader, eval_loss)
          print("Testing loss  ({} prc): {:.5f}".format(self.mc.testing_percentile,
                                                        self.test_loss))
          self.evaluate_model_prediction(self.y_train_df, self.X_test_df,
                                        self.y_test_df, self.y_hat_benchmark, epoch=epoch)
          self.esrnn.train()

    if verbose: print('Train finished! \n')

  def per_series_evaluation(self, dataloader, criterion):
    """
    Auxiliary function, evaluate ESRNN model for training 
    procedure supervision.

    Parameters
    ----------
    dataloader: pytorch dataloader
    criterion: pytorch test criterion
    """

    with torch.no_grad():
      # Create fast dataloader
      new_batch_size = min(self.mc.n_series, self.mc.batch_size_test)
      dataloader.update_batch_size(new_batch_size)

      per_series_losses = []
      for _ in range(dataloader.n_batches):
        batch = dataloader.get_batch()
        windows_y, windows_y_hat, _ = self.esrnn(batch)
        loss = criterion(windows_y, windows_y_hat)
        per_series_losses += loss.data.cpu().numpy().tolist()

      dataloader.update_batch_size(self.mc.batch_size)
    return per_series_losses

  def model_evaluation(self, dataloader, criterion):
    """
    Auxiliary function, evaluate ESRNN model for training 
    procedure supervision.

    Parameters
    ----------
    dataloader: pytorch dataloader
    criterion: pytorch test criterion
    
    Returns
    -------
    model_loss: float
      loss for train supervision purpose.
    """

    with torch.no_grad():
      # Create fast dataloader
      new_batch_size = min(self.mc.n_series, self.mc.batch_size_test)
      dataloader.update_batch_size(new_batch_size)

      model_loss = 0.0
      for _ in range(dataloader.n_batches):
        batch = dataloader.get_batch()
        windows_y, windows_y_hat, _ = self.esrnn(batch)
        loss = criterion(windows_y, windows_y_hat)
        model_loss += loss.data.cpu().numpy()

      model_loss /= dataloader.n_batches
      dataloader.update_batch_size(self.mc.batch_size)
    return model_loss

  def evaluate_model_prediction(self, y_train_df, X_test_df, y_test_df, y_hat_benchmark='y_hat_naive2', epoch=None):
    """
    Evaluate ESRNN model against benchmark in y_test_df

    Parameters
    ----------
    y_train_df: pandas dataframe
      panel with columns 'unique_id', 'ds', 'y'
    X_test_df: pandas dataframe
      panel with columns 'unique_id', 'ds', 'x'
    y_test_df: pandas dataframe
      panel with columns 'unique_id', 'ds', 'y' and a column 
      y_hat_benchmark identifying benchmark predictions
    y_hat_benchmark: str
      column name of benchmark predictions, default y_hat_naive2
    
    Returns
    -------
    model_owa : float
      relative improvement of model with respect to benchmark, measured with 
      the M4 overall weighted average.
    smape: float
      relative improvement of model with respect to benchmark, measured with 
      the symmetric mean absolute percentage error.
    mase: float
      relative improvement of model with respect to benchmark, measured with 
      the M4 mean absolute scaled error.
    """

    assert self._fitted, "Model not fitted yet"

    y_panel = y_test_df.filter(['unique_id', 'ds', 'y'])
    y_benchmark_panel = y_test_df.filter(['unique_id', 'ds', y_hat_benchmark])
    y_benchmark_panel.rename(columns={y_hat_benchmark: 'y_hat'}, inplace=True)
    y_hat_panel = self.predict(X_test_df)
    y_insample = y_train_df.filter(['unique_id', 'ds', 'y'])

    model_owa, model_mase, model_smape = owa(y_panel, y_hat_panel,
                                             y_benchmark_panel, y_insample,
                                             seasonality=self.mc.naive_seasonality)

    if self.min_owa > model_owa:
      self.min_owa = model_owa
      if epoch is not None:
        self.min_epoch = epoch

    print(f'OWA: {np.round(model_owa, 3)} ')
    print(f'SMAPE: {np.round(model_smape, 3)} ')
    print(f'MASE: {np.round(model_mase, 3)} ')

    return model_owa, model_mase, model_smape

  def fit(self, X_df, y_df, X_test_df=None, y_test_df=None, y_hat_benchmark='y_hat_naive2',
          warm_start=False, shuffle=True, verbose=True):
    """
    Fit ESRNN model.

    Parameters
    ----------
    X_df : pandas dataframe
      Train dataframe in long format with columns 'unique_id', 'ds' 
      and 'x'.
      - 'unique_id' an identifier of each independent time series.
      - 'ds' is a datetime column
      - 'x' is a single exogenous variable
    y_df : pandas dataframe
      Train dataframe in long format with columns 'unique_id', 'ds' and 'y'.
      - 'unique_id' an identifier of each independent time series.
      - 'ds' is a datetime column
      - 'y' is the column with the target values
    X_test_df: pandas dataframe
      Optional test dataframe with columns 'unique_id', 'ds' and 'x'.
      If provided the fit procedure will evaluate the intermediate 
      performance within training epochs.
    y_test_df: pandas dataframe
      Optional test dataframe with columns 'unique_id', 'ds' and 'x' and
      y_hat_benchmark column.
      If provided the fit procedure will evaluate the intermediate 
      performance within training epochs.
    y_hat_benchmark: str
      Name of the benchmark model for the comparison of the relative
      improvement of the model.
    
    Returns
    -------
    self : returns an instance of self.
    """

    # Transform long dfs to wide numpy
    assert type(X_df) == pd.core.frame.DataFrame
    assert type(y_df) == pd.core.frame.DataFrame
    assert all(col in X_df for col in ['unique_id', 'ds', 'x'])
    assert all(col in y_df for col in ['unique_id', 'ds', 'y'])
    if y_test_df is not None:
        assert y_hat_benchmark in y_test_df.columns, 'benchmark is not present in y_test_df, use y_hat_benchmark to define it'

    # Storing dfs for OWA evaluation, initializing min_owa
    self.y_train_df = y_df
    self.X_test_df = X_test_df
    self.y_test_df = y_test_df
    self.min_owa = 4.0
    self.min_epoch = 0

    self.int_ds = isinstance(self.y_train_df['ds'][0], (int, np.int, np.int64))

    self.y_hat_benchmark = y_hat_benchmark

    X, y = self.long_to_wide(X_df, y_df)
    assert len(X)==len(y)
    assert X.shape[1]>=3

    # Exogenous variables
    unique_categories = np.unique(X[:, 1])
    self.mc.category_to_idx = {
        word: index
        for index, word in enumerate(unique_categories)
    }
    exogenous_size = len(unique_categories)

    # Create batches (device in mc)
    self.train_dataloader = Iterator(mc=self.mc, X=X, y=y)

    # Random Seeds (model initialization)
    torch.manual_seed(self.mc.random_seed)
    np.random.seed(self.mc.random_seed)

    # Initialize model
    n_series = self.train_dataloader.n_series
    self.instantiate_esrnn(exogenous_size, n_series)

    # Validating frequencies
    X_train_frequency = pd.infer_freq(X_df.head()['ds'])
    y_train_frequency = pd.infer_freq(y_df.head()['ds'])
    self.frequencies = [X_train_frequency, y_train_frequency]

    if (X_test_df is not None) and (y_test_df is not None):
        X_test_frequency = pd.infer_freq(X_test_df.head()['ds'])
        y_test_frequency = pd.infer_freq(y_test_df.head()['ds'])
        self.frequencies += [X_test_frequency, y_test_frequency]

    assert (len(set(self.frequencies)) <=
            1), f"Match the frequencies of the dataframes {self.frequencies}"

    self.mc.frequency = self.frequencies[0]
    print(f"Infered frequency: {self.mc.frequency}")

    # Train model
    self._fitted = True
    self.train(dataloader=self.train_dataloader, max_epochs=self.mc.max_epochs,
               warm_start=warm_start, shuffle=shuffle, verbose=verbose)

  def instantiate_esrnn(self, exogenous_size, n_series):
    """Auxiliary function used at beginning of train to instantiate ESRNN"""
    
    self.mc.exogenous_size = exogenous_size
    self.mc.n_series = n_series
    self.esrnn = _ESRNN(self.mc).to(self.mc.device)

  def predict(self, X_df, decomposition=False):
    """
    Predict using the ESRNN model.

    Parameters
    ----------
    X_df : pandas dataframe
      Dataframe in LONG format with columns 'unique_id', 'ds' 
      and 'x'.
      - 'unique_id' an identifier of each independent time series.
      - 'ds' is a datetime column
      - 'x' is a single exogenous variable

    Returns
    -------
    Y_hat_panel : pandas dataframe
      Dataframe in LONG format with columns 'unique_id', 'ds' 
      and 'x'.
      - 'unique_id' an identifier of each independent time series.
      - 'ds' datetime columnn that matches the dates in X_df
      - 'y_hat' is the column with the predicted target values
    """

    #print(9*'='+' Predicting ESRNN ' + 9*'=' + '\n')
    assert type(X_df) == pd.core.frame.DataFrame
    assert 'unique_id' in X_df
    assert self._fitted, "Model not fitted yet"

    self.esrnn.eval()

    # Create fast dataloader
    new_batch_size = min(self.mc.n_series, self.mc.batch_size_test)
    self.train_dataloader.update_batch_size(new_batch_size)
    dataloader = self.train_dataloader

    # Create Y_hat_panel placeholders
    output_size = self.mc.output_size
    n_unique_id = len(dataloader.sort_key['unique_id'])
    panel_unique_id = pd.Series(dataloader.sort_key['unique_id']).repeat(output_size)

    #access column with last train date
    panel_last_ds = pd.Series(dataloader.X[:, 2])
    panel_ds = []
    for i in range(len(panel_last_ds)):
      ranges = pd.date_range(start=panel_last_ds[i], periods=output_size+1, freq=self.mc.frequency)
      panel_ds += list(ranges[1:])

    panel_y_hat= np.zeros((output_size * n_unique_id))

    # Predict
    count = 0
    for _ in range(dataloader.n_batches):
      batch = dataloader.get_batch()
      batch_size = batch.y.shape[0]

      if self.mc.ensemble:
        y_hat = torch.zeros((5,batch_size,output_size))
        for i in range(5):
          y_hat[i,:,:] = self.esrnn_ensemble[i].predict(batch)
        y_hat = torch.mean(y_hat,0)
      else:
        y_hat = self.esrnn.predict(batch)

      y_hat = y_hat.data.cpu().numpy()

      panel_y_hat[count:count+output_size*batch_size] = y_hat.flatten()
      count += output_size*batch_size

    Y_hat_panel_dict = {'unique_id': panel_unique_id,
                        'ds': panel_ds,
                        'y_hat': panel_y_hat}

    assert len(panel_ds) == len(panel_y_hat) == len(panel_unique_id)

    Y_hat_panel = pd.DataFrame.from_dict(Y_hat_panel_dict)

    if 'ds' in X_df:
      Y_hat_panel = X_df.merge(Y_hat_panel, on=['unique_id', 'ds'], how='left')
    else:
      Y_hat_panel = X_df.merge(Y_hat_panel, on=['unique_id'], how='left')

    self.train_dataloader.update_batch_size(self.mc.batch_size)
    return Y_hat_panel

  def long_to_wide(self, X_df, y_df):
    """
    Auxiliary function to wrangle LONG format dataframes 
    to a wide format compatible with ESRNN inputs.

    Parameters
    ----------
    X_df : pandas dataframe
      Dataframe in long format with columns 'unique_id', 'ds' 
      and 'x'.
      - 'unique_id' an identifier of each independent time series.
      - 'ds' is a datetime column
      - 'x' is a single exogenous variable
    y_df : pandas dataframe
      Dataframe in long format with columns 'unique_id', 'ds' and 'y'.
      - 'unique_id' an identifier of each independent time series.
      - 'ds' is a datetime column
      - 'y' is the column with the target values
    
    Returns
    -------
    X: numpy array, shape (n_unique_ids, n_time)
    y: numpy array, shape (n_unique_ids, n_time)
    """
    data = X_df.copy()
    data['y'] = y_df['y'].copy()
    sorted_ds = np.sort(data['ds'].unique())
    ds_map = {t: dmap for dmap, t in enumerate(sorted_ds)}
    data['ds_map'] = data['ds'].map(ds_map)
    data = data.sort_values(by=['ds_map','unique_id'])
    df_wide = data.pivot(index='unique_id', columns='ds_map')['y']

    x_unique = data[['unique_id', 'x']].groupby('unique_id').first()
    last_ds =  data[['unique_id', 'ds']].groupby('unique_id').last()
    assert len(x_unique)==len(data.unique_id.unique())
    df_wide['x'] = x_unique
    df_wide['last_ds'] = last_ds
    df_wide = df_wide.reset_index().rename_axis(None, axis=1)

    ds_cols = data.ds_map.unique().tolist()
    X = df_wide.filter(items=['unique_id', 'x', 'last_ds']).values
    y = df_wide.filter(items=ds_cols).values

    return X, y

  def get_dir_name(self, root_dir=None):
    """Auxiliary function to save ESRNN model"""
    if not root_dir:
      assert self.mc.root_dir
      root_dir = self.mc.root_dir

    data_dir = self.mc.dataset_name
    model_parent_dir = os.path.join(root_dir, data_dir)
    model_path = [f'esrnn_{str(self.mc.copy)}']
    return os.path.join(model_parent_dir, '_'.join(model_path))

  def save(self, model_dir=None, copy=None):
    """Auxiliary function to save ESRNN model"""
    if copy is not None:
        self.mc.copy = copy

    if not model_dir:
      assert self.mc.root_dir
      model_dir = self.get_dir_name()

    if not os.path.exists(model_dir):
      os.makedirs(model_dir)

    rnn_filepath = os.path.join(model_dir, "rnn.model")
    es_filepath = os.path.join(model_dir, "es.model")

    print(f'Saving model to:\n {model_dir}' + '\n')
    torch.save({'model_state_dict': self.es.state_dict()}, es_filepath)
    torch.save({'model_state_dict': self.rnn.state_dict()}, rnn_filepath)

  def load(self, model_dir=None, copy=None):
    """Auxiliary function to load ESRNN model"""
    if copy is not None:
      self.mc.copy = copy

    if not model_dir:
      assert self.mc.root_dir
      model_dir = self.get_dir_name()

    rnn_filepath = os.path.join(model_dir, "rnn.model")
    es_filepath = os.path.join(model_dir, "es.model")
    path = Path(es_filepath)

    if path.is_file():
      print(f'Loading model from:\n {model_dir}' + '\n')

      checkpoint = torch.load(es_filepath, map_location=self.mc.device)
      self.es.load_state_dict(checkpoint['model_state_dict'])
      self.es.to(self.mc.device)

      checkpoint = torch.load(rnn_filepath, map_location=self.mc.device)
      self.rnn.load_state_dict(checkpoint['model_state_dict'])
      self.rnn.to(self.mc.device)
    else:
      print(f'Model path {path} does not exist')

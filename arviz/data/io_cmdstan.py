"""CmdStan-specific conversion code."""
from collections import defaultdict
from copy import deepcopy
from glob import glob
import linecache
import os
import re


import numpy as np
import pandas as pd
import xarray as xr

from .inference_data import InferenceData
from .base import requires, dict_to_dataset, generate_dims_coords


class CmdStanConverter:
    """Encapsulate CmdStan specific logic."""

    # pylint: disable=too-many-instance-attributes

    def __init__(self, *, output=None, prior=None, posterior_predictive=None,
                 observed_data=None, observed_data_var=None,
                 log_likelihood=None, coords=None, dims=None):
        self.output = sorted(glob(output)) if isinstance(output, str) else output
        if isinstance(output, str) and len(self.output) > 1:
            msg = "\n".join("{}: {}".format(i, os.path.normpath(path)) \
                            for i, path in enumerate(self.output, 1))
            print("glob found {} files for 'output':\n{}".format(len(self.output), msg))
        if isinstance(prior, str):
            prior_glob = glob(prior)
            if len(prior_glob) > 1:
                prior = sorted(prior_glob)
                msg = "\n".join("{}: {}".format(i, os.path.normpath(path)) \
                                for i, path in enumerate(prior, 1))
                print("glob found {} files for 'prior':\n{}".format(len(prior), msg))
        self.prior = prior
        if isinstance(posterior_predictive, str):
            posterior_predictive_glob = glob(posterior_predictive)
            if len(posterior_predictive_glob) > 1:
                posterior_predictive = sorted(posterior_predictive_glob)
                msg = "\n".join("{}: {}".format(i, os.path.normpath(path)) \
                                for i, path in enumerate(posterior_predictive, 1))
                len_pp = len(posterior_predictive)
                print("glob found {} files for 'posterior_predictive':\n{}".format(len_pp, msg))
        self.posterior_predictive = posterior_predictive
        self.observed_data = observed_data
        self.observed_data_var = observed_data_var
        self.log_likelihood = log_likelihood
        self.coords = coords if coords is not None else {}
        self.dims = dims if dims is not None else {}
        self.sample_stats = None
        self.posterior = None
        # populate posterior and sample_Stats
        self._parse_output()

    @requires('output')
    def _parse_output(self):
        """Read csv paths to list of dataframes."""
        chain_data = []
        for path in self.output:
            parsed_output = _read_output(path)
            for sample, sample_stats, config, adaptation, timing  in parsed_output:
                chain_data.append({
                    'sample' : sample,
                    'sample_stats' : sample_stats,
                    'configuration_info' : config,
                    'adaptation_info' : adaptation,
                    'timing_info' : timing,
                })
        self.posterior = [item['sample'] for item in chain_data]
        self.sample_stats = [item['sample_stats'] for item in chain_data]

    @requires('posterior')
    def posterior_to_xarray(self):
        """Extract posterior samples from output csv."""
        columns = self.posterior[0].columns

        # filter posterior_predictive and log_likelihood
        post_pred = self.posterior_predictive
        if post_pred is None or \
           (isinstance(post_pred, str) and post_pred.lower().endswith('.csv')):
            post_pred = []
        elif isinstance(post_pred, str):
            post_pred = [col for col in columns if post_pred == col.split(".")[0]]
        else:
            post_pred = [
                col for col in columns \
                if any(item == col.split(".")[0] for item in post_pred)
            ]

        log_lik = self.log_likelihood
        if log_lik is None:
            log_lik = []
        elif isinstance(log_lik, str):
            log_lik = [col for col in columns if log_lik == col.split('.')[0]]
        else:
            log_lik = [col for col in columns if any(item == col.split('.')[0] for item in log_lik)]

        valid_cols = [col for col in columns if col not in post_pred+log_lik]
        data = _unpack_dataframes([item[valid_cols] for item in self.posterior])
        return dict_to_dataset(data, coords=self.coords, dims=self.dims)

    @requires('sample_stats')
    def sample_stats_to_xarray(self):
        """Extract sample_stats from fit."""
        dtypes = {
            'divergent__' :  bool,
            'n_leapfrog__' : np.int64,
            'treedepth__' :  np.int64,
        }

        sampler_params = self.sample_stats
        log_likelihood = self.log_likelihood
        if isinstance(log_likelihood, str):
            if self.posterior is None:
                # Warning?
                log_likelihood = None
            else:
                log_likelihood_cols = [
                    col for col in self.posterior[0].columns \
                    if log_likelihood == col.split(".")[0]
                ]
                log_likelihood_vals = [
                    item[log_likelihood_cols] for item in self.posterior
                ]

        # copy dims and coords
        dims = deepcopy(self.dims) if self.dims is not None else {}
        coords = deepcopy(self.coords) if self.coords is not None else {}

        if log_likelihood is not None:
            # Add log_likelihood to sampler_params
            for i, _ in enumerate(sampler_params):
                # slice log_likelihood to keep dimensions
                for col in log_likelihood_cols:
                    col_ll = col.replace(log_likelihood, 'log_likelihood')
                    sampler_params[i][col_ll] = log_likelihood_vals[i][col]
            # change dims and coords for log_likelihood if defined
            if isinstance(log_likelihood, str) and log_likelihood in dims:
                dims["log_likelihood"] = dims.pop(log_likelihood)
            if isinstance(log_likelihood, str) and log_likelihood in coords:
                coords["log_likelihood"] = coords.pop(log_likelihood)
        for j, s_params in enumerate(sampler_params):
            rename_dict = {}
            for key in s_params:
                key_, *end = key.split(".")
                name = re.sub('__$', "", key_)
                name = "diverging" if name == 'divergent' else name
                rename_dict[key] = ".".join((name, *end))
                sampler_params[j][key] = s_params[key].astype(dtypes.get(key))
            sampler_params[j] = sampler_params[j].rename(columns=rename_dict)
        data = _unpack_dataframes(sampler_params)
        return dict_to_dataset(data, coords=coords, dims=dims)

    @requires('posterior')
    @requires('posterior_predictive')
    def posterior_predictive_to_xarray(self):
        """Convert posterior_predictive samples to xarray."""
        ppred = self.posterior_predictive

        if isinstance(ppred, (tuple, list)) and ppred[0].endswith(".csv") or \
           isinstance(ppred, str) and ppred.endswith(".csv"):
            chain_data = []
            for path in ppred:
                parsed_output = _read_output(path)
                for sample, *_ in parsed_output:
                    chain_data.append(sample)
            data = _unpack_dataframes(chain_data)
        else:
            if isinstance(ppred, str):
                ppred = [ppred]
            ppred_cols = [
                col for col in self.posterior[0] \
                if any(item == col.split(".")[0] for item in ppred)
            ]
            data = _unpack_dataframes([item[ppred_cols] for item in self.posterior])
        return dict_to_dataset(data, coords=self.coords, dims=self.dims)

    @requires('posterior')
    @requires('prior')
    def prior_to_xarray(self):
        """Convert prior samples to xarray."""
        chains = []
        for path in self.prior:
            parsed_output = _read_output(path)
            for prior, *_ in parsed_output:
                chains.append(prior)
        data = _unpack_dataframes(chains)
        return dict_to_dataset(data, coords=self.coords, dims=self.dims)

    @requires('posterior')
    @requires('observed_data')
    def observed_data_to_xarray(self):
        """Convert observed data to xarray."""
        observed_data_raw = _read_data(self.observed_data)
        variables = self.observed_data_var
        if isinstance(variables, str):
            variables = [variables]
        observed_data = {}
        for key, vals in observed_data_raw.items():
            if variables is not None and key not in variables:
                continue
            vals = np.atleast_1d(vals)
            val_dims = self.dims.get(key)
            val_dims, coords = generate_dims_coords(vals.shape, key,
                                                    dims=val_dims, coords=self.coords)
            observed_data[key] = xr.DataArray(vals, dims=val_dims, coords=coords)
        return xr.Dataset(data_vars=observed_data)

    def to_inference_data(self):
        """Convert all available data to an InferenceData object.

        Note that if groups can not be created (i.e., there is no `output`, so
        the `posterior` and `sample_stats` can not be extracted), then the InferenceData
        will not have those groups.
        """
        return InferenceData(**{
            'posterior': self.posterior_to_xarray(),
            'sample_stats': self.sample_stats_to_xarray(),
            'posterior_predictive' : self.posterior_predictive_to_xarray(),
            'prior' : self.prior_to_xarray(),
            'observed_data' : self.observed_data_to_xarray(),
        })

def _process_configuration(comments):
    """Extract sampling information."""
    num_samples = None
    num_warmup = None
    save_warmup = None
    for comment in comments:
        comment = comment.strip("#").strip()
        if comment.startswith("num_samples"):
            num_samples = int(comment.strip("num_samples = ").strip("(Default)"))
        elif comment.startswith("num_warmup"):
            num_warmup = int(comment.strip("num_warmup = ").strip("(Default)"))
        elif comment.startswith("save_warmup"):
            save_warmup = bool(int(comment.strip("save_warmup = ").strip("(Default)")))
        elif comment.startswith("thin"):
            thin = int(comment.strip("thin = ").strip("(Default)"))

    return {'num_samples'  : num_samples,
            'num_warmup' : num_warmup,
            'save_warmup' : save_warmup,
            'thin' : thin,
           }

def _read_output(path):
    """Read CmdStan output.csv.

    Parameters
    ----------
    path : str

    Returns
    -------
    List[DataFrame, DataFrame, List[str], List[str], List[str]]
        pandas.DataFrame
            Sample data
        pandas.DataFrame
            Sample stats
        List[str]
            Configuration information
        List[str]
            Adaptation information
        List[str]
            Timing info
    """
    chains = []
    configuration_info = []
    adaptation_info = []
    timing_info = []
    i = 0
    # Read (first) configuration and adaption
    with open(path, "r") as f_obj:
        column_names = False
        for i, line in enumerate(f_obj):
            line = line.strip()
            if line.startswith("#"):
                if column_names:
                    adaptation_info.append(line.strip())
                else:
                    configuration_info.append(line.strip())
            elif not column_names:
                column_names = True
                pconf = _process_configuration(configuration_info)
                if pconf['save_warmup']:
                    warmup_range = range(pconf['num_warmup']//pconf['thin'])
                    for _, _ in zip(warmup_range, f_obj):
                        continue
            else:
                break

    # Read data
    with open(path, "r") as f_obj:
        df = pd.read_csv(f_obj, comment="#")

    # split dataframe if header found multiple times
    if df.iloc[:, 0].dtype.kind == 'O':
        first_col = df.columns[0]
        col_locations = first_col == df.loc[:, first_col]
        col_locations = list(col_locations.loc[col_locations].index)
        dfs = []
        for idx, last_idx in zip(col_locations, [-1] + list(col_locations[:-1])):
            df_ = deepcopy(df.loc[last_idx+1:idx-1, :])
            for col in df_.columns:
                df_.loc[:, col] = pd.to_numeric(df_.loc[:, col])
            if len(df_):
                dfs.append(df_.reset_index(drop=True))
            df = df.loc[idx+1:, :]
        for col in df.columns:
            df.loc[:, col] = pd.to_numeric(df.loc[:, col])
        dfs.append(df)
    else:
        dfs = [df]

    for j, df in enumerate(dfs):
        if j == 0:
            # Read timing info (first) from the end of the file
            line_num = i + df.shape[0] + 1
            for k in range(5):
                line = linecache.getline(path, line_num+k).strip()
                if len(line):
                    timing_info.append(line)
            configuration_info_len = len(configuration_info)
            adaptation_info_len = len(adaptation_info)
            timing_info_len = len(timing_info)
            num_of_samples = df.shape[0]
            header_count = 1
            last_line_num = configuration_info_len + adaptation_info_len +\
                            timing_info_len + num_of_samples + header_count
        else:
            # header location found in the dataframe (not first)
            configuration_info = []
            adaptation_info = []
            timing_info = []

            # line number for the next dataframe in csv
            line_num = last_line_num + 1

            # row ranges
            config_start = line_num
            config_end = config_start+configuration_info_len

            # read configuration_info
            for reading_line in range(config_start, config_end):
                line = linecache.getline(path, reading_line)
                if line.startswith("#"):
                    configuration_info.append(line)
                else:
                    msg = "Invalid input file. " \
                          "Header information missing from combined csv. " \
                          "Configuration: {}".format(path)
                    raise ValueError(msg)

            pconf = _process_configuration(configuration_info)
            warmup_rows = pconf['save_warmup']*pconf['num_warmup']//pconf['thin']
            adaption_start = config_end + 1 + warmup_rows
            adaption_end = adaption_start + adaptation_info_len
            # read adaptation_info
            for reading_line in range(adaption_start, adaption_end):
                line = linecache.getline(path, reading_line)
                if line.startswith("#"):
                    adaptation_info.append(line)
                else:
                    msg = "Invalid input file. " \
                          "Header information missing from combined csv. " \
                          "Adaptation: {}".format(path)
                    raise ValueError(msg)

            timing_start = adaption_end + len(df) - warmup_rows
            timing_end = timing_start + timing_info_len
            # read timing_info
            for reading_line in range(timing_start, timing_end):
                line = linecache.getline(path, reading_line)
                if line.startswith("#"):
                    timing_info.append(line)
                else:
                    msg = "Invalid input file. " \
                          "Header information missing from combined csv. " \
                          "Timing: {}".format(path)
                    raise ValueError(msg)
            last_line_num = reading_line

        # Remove warmup
        if pconf['save_warmup']:
            saved_samples = pconf['num_samples']//pconf['thin']
            df = df.iloc[-saved_samples:, :]

        # Split data to sample_stats and sample
        sample_stats_columns = [col for col in df.columns if col.endswith("__")]
        sample_columns = [col for col in df.columns if col not in sample_stats_columns]

        sample_stats = df.loc[:, sample_stats_columns]
        sample_df = df.loc[:, sample_columns]

        chains.append((sample_df, sample_stats, configuration_info, adaptation_info, timing_info))

    return chains

def _process_data_var(string):
    """Transform datastring to key, values pair.

    All values are transformed to floating point values.

    Parameters
    ----------
    string : str

    Returns
    -------
    Tuple[Str, Str]
        key, values pair
    """
    key, var = string.split("<-")
    if 'structure' in var:
        var, dim = var.replace("structure(", "").replace(",", "").split(".Dim")
        #dtype = int if '.' not in var and 'e' not in var.lower() else float
        dtype = float
        var = var.replace("c(", "").replace(")", "").strip().split()
        dim = dim.replace("=", "").replace("c(", "").replace(")", "").strip().split()
        dim = tuple(map(int, dim))
        var = np.fromiter(map(dtype, var), dtype).reshape(dim, order='F')
    elif 'c(' in var:
        #dtype = int if '.' not in var and 'e' not in var.lower() else float
        dtype = float
        var = var.replace("c(", "").replace(")", "").split(",")
        var = np.fromiter(map(dtype, var), dtype)
    else:
        #dtype = int if '.' not in var and 'e' not in var.lower() else float
        dtype = float
        var = dtype(var)
    return key.strip(), var

def _read_data(path):
    """Read Rdump output and transform to Python dictionary.

    Parameters
    ----------
    path : str

    Returns
    -------
    Dict
        key, values pairs from Rdump formatted data.
    """
    data = {}
    with open(path, "r") as f_obj:
        var = ""
        for line in f_obj:
            if '<-' in line:
                if len(var):
                    key, var = _process_data_var(var)
                    data[key] = var
                var = ""
            var += " " + line.strip()
        if len(var):
            key, var = _process_data_var(var)
            data[key] = var
    return data

def _unpack_dataframes(dfs):
    """Transform a list of pandas.DataFrames to dictionary containing ndarrays.

    Parameters
    ----------
    dfs : List[pandas.DataFrame]

    Returns
    -------
    Dict
        key, values pairs. Values are formatted to shape = (nchain, ndraws, *shape)
    """
    col_groups = defaultdict(list)
    columns = dfs[0].columns
    for col in columns:
        key, *loc = col.split('.')
        loc = tuple(int(i) - 1 for i in loc)
        col_groups[key].append((col, loc))

    chains = len(dfs)
    draws = len(dfs[0])
    sample = {}
    for key, cols_locs in col_groups.items():
        ndim = np.array([loc for _, loc in cols_locs]).max(0) + 1
        sample[key] = np.full((chains, draws, *ndim), np.nan)
        for col, loc in cols_locs:
            for chain_id, df in enumerate(dfs):
                draw = df[col].values
                if loc == ():
                    sample[key][chain_id, :] = draw
                else:
                    axis1_all = range(sample[key].shape[1])
                    slicer = (chain_id, axis1_all, *loc)
                    sample[key][slicer] = draw
    return sample

def from_cmdstan(*, output=None, prior=None, posterior_predictive=None,
                 observed_data=None, observed_data_var=None,
                 log_likelihood=None, coords=None, dims=None):
    """Convert CmdStan data into an InferenceData object.

    Parameters
    ----------
    output : List[str]
        List of paths to output.csv files.
        CSV file can be stacked csv containing all the chains

            cat output*.csv > combined_output.CSV

    prior : List[str]
        List of paths to output.csv files
        CSV file can be stacked csv containing all the chains.

            cat output*.csv > combined_output.CSV

    posterior_predictive : str, List[Str]
        Posterior predictive samples for the fit. If endswith ".csv" assumes file.
    observed_data : str
        Observed data used in the sampling. Path to data file in Rdump format.
    observed_data_var : str, List[str]
        Variable(s) used for slicing observed_data. If not defined, all
        data variables are imported.
    log_likelihood : str
        Pointwise log_likelihood for the data.
    coords : dict[str, iterable]
        A dictionary containing the values that are used as index. The key
        is the name of the dimension, the values are the index values.
    dims : dict[str, List(str)]
        A mapping from variables to a list of coordinate names for the variable.

    Returns
    -------
    InferenceData object
    """
    return CmdStanConverter(
        output=output,
        prior=prior,
        posterior_predictive=posterior_predictive,
        observed_data=observed_data,
        observed_data_var=observed_data_var,
        log_likelihood=log_likelihood,
        coords=coords,
        dims=dims).to_inference_data()
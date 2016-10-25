import logging
import os
from typing import Any, Dict, List

import numpy
from keras.models import model_from_json

from . import concrete_pretrainers
from ..common.checks import ConfigurationError
from ..common.params import get_choice
from ..data.dataset import Dataset
from ..data.instances.instance import Instance
from ..layers.wrappers import OutputMask
from .models import DeepQaModel
from .optimizers import optimizer_from_params

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Trainer:
    """
    A Trainer object specifies data, a model, and a way to train the model with the data.  Here we
    group all of the common code related to these things, making only minimal assumptions about
    what kind of data you're using or what the structure of your model is.

    The main benefits of this class are having a common place for setting parameters related to
    training, actually running the training with those parameters, and code for saving and loading
    models.
    """
    def __init__(self, params: Dict[str, Any]):
        self.name = "Trainer"

        # Should we save the models that we train?  If this is True, you are required to also set
        # the model_serialization_prefix parameter, or the code will crash.
        self.save_models = params.pop('save_models', True)

        # Prefix for saving and loading model files
        self.model_prefix = params.pop('model_serialization_prefix', None)
        if self.model_prefix:
            parent_directory = os.path.dirname(self.model_prefix)
            os.makedirs(parent_directory, exist_ok=True)

        # Upper limit on the number of training instances.  If this is set, and we get more than
        # this, we will truncate the data.
        self.max_training_instances = params.pop('max_training_instances', None)
        # Amount of training data to use for Keras' validation (not our QA validation, set by
        # the validation_file param, which is separate).  This value is passed as
        # 'validation_split' to Keras' model.fit().
        self.keras_validation_split = params.pop('keras_validation_split', 0.1)
        # Number of train epochs.
        self.num_epochs = params.pop('num_epochs', 20)
        # Number of epochs to be patient before early stopping.
        self.patience = params.pop('patience', 1)

        # The files containing the data that should be used for training.  See
        # _load_dataset_from_files().
        self.train_files = params.pop('train_files', None)

        # The files containing the data that should be used for validation, if you do not want to
        # use a split of the training data for validation.  The default of None means to just use
        # the `keras_validation_split` parameter to split the training data for validation.
        self.validation_files = params.pop('validation_files', None)

        optimizer_params = params.pop('optimizer', 'adam')
        self.optimizer = optimizer_from_params(optimizer_params)
        self.loss = params.pop('loss', 'categorical_crossentropy')
        self.metrics = params.pop('metrics', ['accuracy'])
        self.validation_metric = params.pop('validation_metric', 'val_acc')

        # This is a debugging setting, mostly - we have written a custom model.summary() method
        # that supports showing masking info, to help understand what's going on with the masks.
        self.show_summary_with_masking = params.pop('show_summary_with_masking_info', False)

        # This should be a dict, containing the following keys:
        # - "layer_names", which has as a value a list of names that must match layer names in the
        #     model build by this Trainer.
        # - "data", which has as a value either "training", "validation", or a list of file names.
        #     If you give "training" or "validation", we'll use those datasets, otherwise we'll
        #     load data from the provided files.  Note that currently "validation" only works if
        #     you provide validation files, not if you're just using Keras to split the training
        #     data.
        # - "masks", an optional key that functions identically to "layer_names", except we output
        #     the mask at each layer given here.
        self.debug_params = params.pop('debug', {})

        pretrainer_params = params.pop('pretrainers', [])
        self.pretrainers = []
        for pretrainer_param in pretrainer_params:
            pretrainer_type = get_choice(pretrainer_param, "type", concrete_pretrainers.keys())
            pretrainer = concrete_pretrainers[pretrainer_type](self, pretrainer_param)
            self.pretrainers.append(pretrainer)

        # We've now processed all of the parameters, and we're the base class, so there should not
        # be anything left.
        if len(params.keys()) != 0:
            raise ConfigurationError("You passed unrecognized parameters: " + str(params))

        # Model-specific member variables that will get set and used later.
        self.model = None
        self.debug_model = None

        # Training-specific member variables that will get set and used later.
        self.best_epoch = -1

        # We store the datasets used for training and validation, both before processing and after
        # processing, in case a subclass wants to modify it between epochs for whatever reason.
        self.training_dataset = None
        self.train_input = None
        self.train_labels = None
        self.validation_dataset = None
        self.validation_input = None
        self.validation_labels = None
        self.debug_dataset = None
        self.debug_input = None

    def can_train(self):
        return self.train_files is not None

    def _load_dataset_from_files(self, files: List[str]) -> Dataset:
        """
        Given a list of file inputs, load a raw dataset from the files.  This is a list because
        some datasets are specified in more than one file (e.g., a file containing the instances,
        and a file containing background information about those instances).
        """
        raise NotImplementedError

    def _prepare_data(self, dataset: Dataset, for_train: bool):
        """
        Takes a raw dataset and converts it into training inputs and labels that can be used to
        either train a model or make predictions.

        Input: a Dataset of the same format as read by the read_dataset_from_files() method, and an
        indicator for whether this is being done for the training data (so that, e.g., you can set
        model parameters based on characteristics of the training data).

        Output: a tuple of (inputs, labels), which can be fed directly to Keras' model.fit()
        and model.predict() methods.  `labels` is allowed to be None in the second case.
        """
        raise NotImplementedError

    def _prepare_instance(self, instance: Instance, make_batch: bool=True):
        """
        Like self._prepare_data(), but for a single Instance.  Used most often for making
        predictions one at a time on test data (though you should avoid that if possible, as larger
        batches would be more efficient).

        The make_batch argument determines whether we make the return value into a batch or not by
        calling numpy.expand_dims.  Keras' model.predict() method requires a batch, so we need an
        extra dimension.  If you're going to do the batch conversion yourself, or don't need it,
        you can pass False for that parameter.
        """
        raise NotImplementedError

    def _pretrain(self):
        """
        Runs whatever pre-training has been specified in the constructor.
        """
        logger.info("Running pre-training")
        for pretrainer in self.pretrainers:
            pretrainer.train()
        self._pretraining_finished_hook()

    def _process_pretraining_data(self):
        """
        Processes the pre-training data in whatever way you want, typically for setting model
        parameters like vocabulary.  This happens _before_ the training data itself is processed.
        We don't know what processing this might entail, or whether you are even doing any
        pre-training, so we just pass here by default.
        """
        pass

    def _pretraining_finished_hook(self):
        """
        This is called when pre-training finishes (if there were any pre-trainers specified).  You
        can do whatever you want in here, like changing model parameters based on what happened
        during pre-training, or saving a pre-trained model, or whatever.

        Default implementation is to call pretrainer._on_finished() for each pre-trainer, which by
        default is a `pass`.
        """
        for pretrainer in self.pretrainers:
            pretrainer.on_finished()

    def _compile_kwargs(self):
        """
        Because we call model.compile() in a few different places in the code, and we have a few
        member variables that we use to set arguments for model.compile(), we group those arguments
        together here, to only specify them once.
        """
        return {
                'loss': self.loss,
                'optimizer': self.optimizer,
                'metrics': self.metrics,
                }

    def _build_model(self) -> DeepQaModel:
        """Constructs and returns a DeepQaModel (which is a wrapper around a Keras Model) that will
        take the output of self._get_training_data as input, and produce as output a true/false
        decision for each input.

        The returned model will be used to call model.fit(train_input, train_labels).
        """
        raise NotImplementedError

    def train(self):
        '''
        Trains the model.

        All training parameters have already been passed to the constructor, so we need no
        arguments to this method.
        '''
        logger.info("Running training (%s)", self.name)

        # Before actually doing any training, we'll run whatever pre-training has been specified.
        # Note that this can have funny interactions with model parameters that get fit to the
        # training data.  We don't really know here what you want to do with the data you have for
        # pre-training, if any, so we provide a hook that you can override to do whatever you want.
        if self.pretrainers:
            self._process_pretraining_data()

        # First we need to prepare the data that we'll use for training.
        logger.info("Getting training data")
        self.training_dataset = self._load_dataset_from_files(self.train_files)
        if self.max_training_instances is not None:
            logger.info("Truncating the training dataset to %d instances", self.max_training_instances)
            self.training_dataset = self.training_dataset.truncate(self.max_training_instances)
        self.train_input, self.train_labels = self._prepare_data(self.training_dataset, for_train=True)
        if self.validation_files:
            logger.info("Getting validation data")
            self.validation_dataset = self._load_dataset_from_files(self.validation_files)
            self.validation_input, self.validation_labels = self._prepare_data(self.validation_dataset,
                                                                               for_train=False)

        # We need to actually do pretraining _after_ we've loaded the training data, though, as we
        # need to build the models to be consistent between training and pretraining.  The training
        # data tells us a max sentence length, which we need for the pretrainer.
        if self.pretrainers:
            self._pretrain()

        # Then we build the model and compile it.
        logger.info("Building the model")
        self.model = self._build_model()
        self.model.summary(show_masks=self.show_summary_with_masking)
        self.model.compile(**self._compile_kwargs())

        if self.debug_params:
            # Get the list of layers whose outputs will be visualized as per the
            # solver definition and build a debug model.
            debug_layer_names = self.debug_params['layer_names']
            debug_masks = self.debug_params.get('masks', [])
            debug_data = self.debug_params['data']
            if debug_data == "training":
                self.debug_dataset = self.training_dataset
                self.debug_input = self.train_input
            elif debug_data == "validation":
                # NOTE: This currently only works if you've specified specific validation data, not
                # if you are just splitting the training data for validation.
                self.debug_dataset = self.validation_dataset
                self.debug_input = self.validation_input
            else:
                # If the `data` param is not "training" or "validation", we assume it's a list of
                # file names.
                self.debug_dataset = self._load_dataset_from_files(debug_data)
                self.debug_input, _ = self._prepare_data(self.debug_dataset, for_train=False)
            self.debug_model = self._build_debug_model(debug_layer_names, debug_masks)
            self.debug_model.compile(loss='mse', optimizer='sgd')  # Will not train this model.


        # Now we actually train the model, with patient early stopping using the validation data.
        best_accuracy = 0.0
        self.best_epoch = 0
        num_worse_epochs = 0
        for epoch_id in range(self.num_epochs):
            self._pre_epoch_hook(epoch_id)
            logger.info("Epoch %d", epoch_id)
            kwargs = {'nb_epoch': 1}
            # We'll check for explicit validation data first; if you provided this, you definitely
            # wanted to use it for validation.  self.keras_validation_split is non-zero by default,
            # so you may have left it above zero on accident.
            if self.validation_input:
                kwargs['validation_data'] = (self.validation_input, self.validation_labels)
            elif self.keras_validation_split > 0.0:
                kwargs['validation_split'] = self.keras_validation_split
            history = self.model.fit(self.train_input, self.train_labels, **kwargs)
            self._post_epoch_hook(epoch_id, history.history)
            accuracy = history.history[self.validation_metric][-1]
            if accuracy < best_accuracy:
                num_worse_epochs += 1
                if num_worse_epochs >= self.patience:
                    logger.info("Stopping training")
                    break
            else:
                best_accuracy = accuracy
                self.best_epoch = epoch_id
                num_worse_epochs = 0  # Reset the counter.
                if self.save_models:
                    self._save_model(epoch_id)
            if self.debug_params:
                self._debug(debug_layer_names, debug_masks, epoch_id)
        if self.save_models:
            self._save_best_model()

    def _debug(self, debug_layer_names: List[str], debug_masks: List[str], epoch: int):
        """
        Runs the debug model and saves the results to a file.
        """
        logger.info("Running debug model")
        # Shows intermediate outputs of the model on validation data
        outputs = self.debug_model.predict(self.debug_input)
        output_dict = {}
        if len(debug_layer_names) == 1:
            output_dict[debug_layer_names[0]] = outputs
        else:
            for layer_name, output in zip(debug_layer_names, outputs[:len(debug_layer_names)]):
                output_dict[layer_name] = output
        for layer_name, output in zip(debug_masks, outputs[len(debug_layer_names):]):
            if 'masks' not in output_dict:
                output_dict['masks'] = {}
            output_dict['masks'][layer_name] = output
        self._output_debug_info(output_dict, epoch)

    def _output_debug_info(self, output_dict: Dict[str, numpy.array], epoch: int):
        logger.info("Outputting debug results")
        debug_output_file = open("%s_debug_%d.txt" % (self.model_prefix, epoch), "w")
        overall_debug_info = self._overall_debug_output(output_dict)
        debug_output_file.write(overall_debug_info)
        for instance_index, instance in enumerate(self.debug_dataset.instances):
            instance_output_dict = {}
            for layer_name, output in output_dict.items():
                instance_output_dict[layer_name] = output[instance_index]
            instance_info = self._instance_debug_output(instance, instance_output_dict)
            debug_output_file.write(instance_info + '\n')
        debug_output_file.close()

    def _pre_epoch_hook(self, epoch: int):
        """
        This method gets called before each epoch of training.  If you want to do any kind of
        processing in between epochs (e.g., updating the training data for whatever reason), here
        is your chance to do so.
        """
        pass

    def _post_epoch_hook(self, epoch: int, history: Dict[str, Any]):
        """
        This method gets called directly after model.fit(), before making any early stopping
        decisions.  If you want to modify anything after each iteration (e.g., computing a
        different kind of validation loss to use for early stopping, or just computing and printing
        accuracy on some other held out data), you can do that here.

        If you have good reason to, feel free to modify the history object, which could change the
        behavior of the training (e.g., by setting a new value for the self.validation_metric key,
        which affects early stopping and best epoch decisions).
        """
        pass

    def _build_debug_model(self, debug_layer_names: List[str], debug_masks: List[str]):
        """
        Here we build a very simple kind of debug model: one that takes the same inputs as
        self.model, and runs the model up to some particular layers, and outputs the values at
        those layers.

        In addition, you can optionally specify some number of layers for which you want to output
        the mask computed by that layer.

        If you want something more complicated, override this method.
        """
        debug_inputs = self.model.get_input_at(0)  # list of all input_layers
        debug_output_dict = {}
        layer_names = set(debug_layer_names)
        mask_names = set(debug_masks)
        for layer in self.model.layers:
            if layer.name in layer_names:
                debug_output_dict[layer.name] = layer.get_output_at(0)
                layer_names.remove(layer.name)
            if layer.name in mask_names:
                mask = OutputMask()(layer.get_output_at(0))
                debug_output_dict['mask_for_' + layer.name] = mask
                mask_names.remove(layer.name)
        if len(layer_names) != 0 or len(mask_names):
            raise ConfigurationError("Unmatched debug layer names: " + str(layer_names + mask_names))
        # The outputs need to be in the same order as `debug_layer_names`, or downstream code will
        # have issues.
        debug_outputs = [debug_output_dict[name] for name in debug_layer_names]
        debug_outputs.extend([debug_output_dict['mask_for_' + name] for name in debug_masks])
        return DeepQaModel(input=debug_inputs, output=debug_outputs)

    def _overall_debug_output(self, output_dict: Dict[str, numpy.array]) -> str:
        # pylint: disable=unused-argument
        return "Number of instances: %d\n" % len(self.debug_dataset.instances)

    def _instance_debug_output(self, instance: Instance, outputs: Dict[str, numpy.array]) -> str:
        """
        This method takes an Instance and all of the debug outputs for that Instance, puts them
        into some human-readable format, and returns that as a string.  `outputs` will have one key
        corresponding to each item in the `debug.layer_names` parameter given to the constructor of
        this object.

        The default here is `pass` instead of `raise NotImplementedError`, because you're not
        required to implement debugging for your model.
        """
        pass

    def score_dataset(self, dataset: Dataset):
        inputs, _ = self._prepare_data(dataset, False)
        return self.model.predict(inputs)

    def score_instance(self, instance: Instance):
        inputs, _ = self._prepare_instance(instance)
        try:
            return self.model.predict(inputs)
        except:
            print('Inputs were: ' + str(inputs))
            raise

    def load_model(self, epoch: int=None):
        """
        Loads a serialized model.  If epoch is not None, we try to load the model from that epoch.
        If epoch is not given, we load the best saved model.

        Paths in here must match those in self._save_model(epoch) and self._save_best_model(), or
        things will break.
        """
        logger.info("Loading serialized model")
        # Loading serialized model
        model_config_file = open("%s_config.json" % self.model_prefix)
        model_config_json = model_config_file.read()
        model_config_file.close()
        self.model = model_from_json(model_config_json,
                                     custom_objects=self._get_custom_objects())
        if epoch is not None:
            model_file = "%s_weights_epoch=%d.h5" % (self.model_prefix, epoch)
        else:
            model_file = "%s_weights.h5" % self.model_prefix
        logger.info("Loading weights from file %s", model_file)
        self.model.load_weights(model_file)
        self.model.summary(show_masks=self.show_summary_with_masking)
        self._load_layers()
        self._load_auxiliary_files()
        self._set_params_from_model()
        self.model.compile(**self._compile_kwargs())

    def _load_layers(self):
        """
        If you want to use member variables that contain Layers after the model is loaded, you need
        to set them from the model.  For instance, say you have an embedding layer for word
        sequences, and you want to take a loaded model, build a sub-model out of it that contains
        the embedding layer, and use that model somehow.  In that case, the member variable for the
        embedding layer needs to be set from the loaded model.  You can do that here.
        """
        pass

    def _load_auxiliary_files(self):
        """
        Called during model loading.  If you have some auxiliary pickled object, such as an object
        storing the vocabulary of your model, you can load it here.
        """
        pass

    def _set_params_from_model(self):
        """
        Called after a model is loaded, this lets you update member variables that contain model
        parameters, like max sentence length, that are not stored as weights in the model object.
        This is necessary if you want to process a new data instance to be compatible with the
        model for prediction, for instance.
        """
        pass

    def _save_model(self, epoch: int):
        """
        Serializes the model at a particular epoch for future use.
        """
        model_config = self.model.to_json()
        model_config_file = open("%s_config.json" % (self.model_prefix), "w")
        print(model_config, file=model_config_file)
        model_config_file.close()
        self.model.save_weights("%s_weights_epoch=%d.h5" % (self.model_prefix, epoch), overwrite=True)
        self._save_auxiliary_files()

    def _save_best_model(self):
        """
        Copies the weights from the best epoch to a final weight file

        The point of this is so that the input/output spec of the NNSolver is simpler.  Someone
        calling this as a subroutine doesn't have to worry about which epoch ended up being the
        best, they can just use the final weight file.  You can still use models from other epochs
        if you really want to.
        """
        from shutil import copyfile
        epoch_weight_file = "%s_weights_epoch=%d.h5" % (self.model_prefix, self.best_epoch)
        final_weight_file = "%s_weights.h5" % self.model_prefix
        copyfile(epoch_weight_file, final_weight_file)

    def _save_auxiliary_files(self):
        """
        Called while saving a model.  If you have some auxiliary object, such as an object storing
        the vocabulary of your model, you can save it here.
        """
        pass

    @classmethod
    def _get_custom_objects(cls):
        """
        If you've used any Layers that Keras doesn't know about, you need to specify them in this
        dictionary, so we can load them correctly.
        """
        return {}
import tempfile
import os

import torch

from nnfabrik.utility.dj_helpers import make_hash
from . import integration


class TrainedEnsembleModelHandler:
    def __init__(self, ensemble_facade):
        self.ensemble_facade = ensemble_facade

    def create_ensemble(self, key):
        """Creates a new ensemble and inserts it into the table.

        Args:
            key: A dictionary representing a key that must be sufficient to restrict the dataset table to one entry. The
                models that are in the trained model table after restricting it with the provided key will be part of
                the ensemble.

        Returns:
            None.
        """
        if not self.ensemble_facade.properly_restricts(key):
            raise ValueError("Provided key not sufficient to restrict dataset table to one entry!")
        dataset_key = self.ensemble_facade.fetch_primary_dataset_key(key)
        models = self.ensemble_facade.fetch_trained_models_primary_keys(key)
        ensemble_table_key = dict(dataset_key, ensemble_hash=integration.hash_list_of_dictionaries(models))
        self.ensemble_facade.insert_ensemble(ensemble_table_key)
        self.ensemble_facade.insert_members([{**ensemble_table_key, **m} for m in models])

    def load_model(self, key=None):
        """Wrapper to preserve the interface of the trained model table."""
        return self._load_ensemble_model(key=key)

    def _load_ensemble_model(self, key=None):
        def ensemble_model(x, *args, **kwargs):
            outputs = [m(x, *args, **kwargs) for m in models]
            mean_output = torch.stack(outputs, dim=0).mean(dim=0)
            return mean_output

        model_keys = self.ensemble_facade.fetch_trained_models(key)
        dataloaders, models = tuple(list(x) for x in zip(*[self.ensemble_facade.load_model(key=k) for k in model_keys]))
        for model in models:
            model.eval()
        return dataloaders[0], ensemble_model


class CSRFV1SelectorHandler:
    def __init__(self, table):
        self.table = table

    def make(self, key):
        dataset_config = (self.table.dataset_table & key).fetch1("dataset_config")
        mappings = integration.get_mappings(dataset_config, key)
        self.table.insert(mappings)

    def get_output_selected_model(self, model, key):
        neuron_pos, session_id = (self.table & key).fetch1("neuron_position", "session_id")
        return integration.get_output_selected_model(neuron_pos, session_id, model)


class MEIMethodHandler:
    def __init__(self, mei_method_facade):
        self.mei_method_facade = mei_method_facade

    def add_method(self, method_fn, method_config):
        self.mei_method_facade.insert_method(
            dict(method_fn=method_fn, method_hash=make_hash(method_config), method_config=method_config)
        )

    def generate_mei(self, dataloader, model, key, import_func=integration.import_module):
        method_fn, method_config = self.mei_method_facade.fetch_method(key)
        method_fn = import_func(method_fn)
        mei, evaluations = method_fn(dataloader, model, method_config)
        return dict(key, evaluations=evaluations, mei=mei)


class MEIHandler:
    def __init__(self, mei_facade, model_loader=integration.ModelLoader, cache_size_limit=10):
        self.mei_facade = mei_facade
        self.model_loader = model_loader(mei_facade.trained_model_table, cache_size_limit=cache_size_limit)

    def make(self, key, save_func=torch.save, temp_dir_func=tempfile.TemporaryDirectory):
        dataloaders, model = self.model_loader.load(key=key)
        output_selected_model = self.mei_facade.get_output_selected_model(model, key)
        mei_entity = self.mei_facade.generate_mei(dataloaders, output_selected_model, key)
        self._insert_mei(mei_entity, save_func, temp_dir_func)

    def _insert_mei(self, mei_entity, save_func, temp_dir_func):
        """Saves the MEI to a temporary directory and inserts the prepared entity into the table."""
        mei = mei_entity.pop("mei").squeeze()
        filename = make_hash(mei_entity) + ".pth.tar"
        with temp_dir_func() as temp_dir:
            filepath = os.path.join(temp_dir, filename)
            save_func(mei, filepath)
            mei_entity["mei"] = filepath
            self.mei_facade.insert_mei(mei_entity)

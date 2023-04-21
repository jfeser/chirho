import collections
from typing import Any, Dict, Hashable, List, Optional

import pyro
import torch

from causal_pyro.indexed.internals import _LazyPlateMessenger, get_sample_msg_device


class IndexPlatesMessenger(pyro.poutine.messenger.Messenger):
    plates: Dict[Hashable, pyro.poutine.indep_messenger.IndepMessenger]
    first_available_dim: int

    def __init__(self, first_available_dim: Optional[int] = None):
        if first_available_dim is None:
            first_available_dim = -5  # conservative default for 99% of models
        assert first_available_dim < 0
        self._orig_dim = first_available_dim
        self.first_available_dim = first_available_dim
        self.plates = collections.OrderedDict()
        super().__init__()

    def __enter__(self):
        assert not self.plates
        assert self.first_available_dim == self._orig_dim
        return super().__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        for name in reversed(list(self.plates.keys())):
            self.plates.pop(name).__exit__(exc_type, exc_value, traceback)
        self.first_available_dim = self._orig_dim
        return super().__exit__(exc_type, exc_value, traceback)

    def _pyro_get_index_plates(self, msg):
        msg["value"] = {name: plate.frame for name, plate in self.plates.items()}
        msg["done"], msg["stop"] = True, True

    def _enter_index_plate(self, plate: _LazyPlateMessenger) -> _LazyPlateMessenger:
        try:
            plate.__enter__()
        except ValueError as e:
            if "collide at dim" in str(e):
                raise ValueError(
                    f"{self} was unable to allocate an index plate dimension "
                    f"at dimension {self.first_available_dim}.\n"
                    f"Try setting a value less than {self._orig_dim} for `first_available_dim` "
                    "that is less than the leftmost (most negative) plate dimension in your model."
                )
            else:
                raise e
        stack: List[pyro.poutine.messenger.Messenger] = pyro.poutine.runtime._PYRO_STACK
        stack.pop(stack.index(plate))
        stack.insert(stack.index(self) + len(self.plates) + 1, plate)
        return plate

    def _pyro_add_indices(self, msg):
        (indexset,) = msg["args"]
        for name, indices in indexset.items():
            if name not in self.plates:
                new_size = max(max(indices) + 1, len(indices))
                # Push the new plate onto Pyro's handler stack at a location
                # adjacent to this IndexPlatesMessenger instance so that
                # any handlers pushed after this IndexPlatesMessenger instance
                # are still guaranteed to exit safely in the correct order.
                self.plates[name] = self._enter_index_plate(
                    _LazyPlateMessenger(
                        name=name, dim=self.first_available_dim, size=new_size
                    )
                )
                self.first_available_dim -= 1
            else:
                assert (
                    0
                    <= min(indices)
                    <= len(indices) - 1
                    <= max(indices)
                    < self.plates[name].size
                ), f"cannot add {name}={indices} to {self.plates[name].size}"


class DependentMaskMessenger(pyro.poutine.messenger.Messenger):
    """
    Abstract base class for effect handlers that select a subset of worlds.
    """

    def get_mask(
        self,
        dist: pyro.distributions.Distribution,
        value: Optional[torch.Tensor],
        device: torch.device = torch.device("cpu"),
    ) -> torch.Tensor:
        raise NotImplementedError

    def _pyro_sample(self, msg: Dict[str, Any]) -> None:
        device = get_sample_msg_device(msg["fn"], msg["value"])
        mask = self.get_mask(msg["fn"], msg["value"], device=device)
        msg["mask"] = mask if msg["mask"] is None else msg["mask"] & mask
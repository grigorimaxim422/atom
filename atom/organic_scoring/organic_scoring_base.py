import asyncio
from abc import ABC, abstractmethod
from typing import Any, Literal, Optional, Sequence, Union, Tuple, Callable

import bittensor as bt

from atom.organic_scoring.organic_queue import OrganicQueue, OrganicQueueBase
from atom.organic_scoring.synth_dataset import SynthDatasetBase
from atom.organic_scoring.utils import is_overridden


class OrganicScoringBase(ABC):
    def __init__(
        self,
        axon: bt.axon,
        synth_dataset: Optional[Union[SynthDatasetBase, Sequence[SynthDatasetBase]]],
        trigger_frequency: Union[float, int],
        trigger: Literal["seconds", "steps"],
        trigger_frequency_min: Union[float, int] = 2,
        trigger_scaling_factor: Union[float, int] = 5,
        organic_queue: Optional[OrganicQueueBase] = None,
    ):
        """Runs the organic weight setter task in separate threads.

        Args:
            axon: The axon to use, must be started and served.
            synth_dataset: The synthetic dataset(s) to use, must be inherited from `synth_dataset.SynthDatasetBase`.
                If None, only organic data will be used, when available.
            trigger_frequency: The frequency to trigger the organic scoring reward step.
            trigger: The trigger type, available values: "seconds", "steps".
                In case of "seconds" the `trigger_frequency` is the number of seconds to wait between each step.
                In case of "steps" the `trigger_frequency` is the number of steps to wait between each step. The
                `increment_step` method should be called to increment the step counter.
            organic_queue: The organic queue to use, must be inherited from `organic_queue.OrganicQueueBase`.
                Defaults to `organic_queue.OrganicQueue`.
            trigger_frequency_min: The minimum frequency value to trigger the organic scoring reward step.
                Defaults to 1.
            trigger_scaling_factor: The scaling factor to adjust the trigger frequency based on the size
                of the organic queue. A higher value means that the trigger frequency adjusts more slowly to changes
                in the organic queue size. This value must be greater than 0.

        Override the following methods:
            - `forward`: Method to establish the sampling logic for the organic scoring task.
            - `_on_organic_entry`: Handle an organic entry, append required values to `_organic_queue`.
                Important: this method must add the required values to the `_organic_queue`.
            - `_query_miners`: Query the miners with a given organic sample.
            - `_set_weights`: Set the weights based on generated rewards for the miners.
            - (Optional) `_priority_fn`: Function with priority value for organic handles.
            - (Optional) `_blacklist_fn`: Function with blacklist for organic handles.
            - (Optional) `_verify_fn`: Function to verify requests for organic handles.

        Usage:
            1. Create a subclass of OrganicScoringBase.
            2. Implement the required methods.
            3. Create an instance of the subclass.
            4. Call the `start` method to start the organic scoring task.
            5. Call the `stop` method to stop the organic scoring task.
            6. Call the `increment_step` method to increment the step counter if the trigger is set to "steps".
        """
        self._axon = axon
        self._should_exit = False
        self._is_running = False
        self._synth_dataset = synth_dataset

        if isinstance(self._synth_dataset, SynthDatasetBase):
            self._synth_dataset = (synth_dataset,)

        self._trigger_frequency = trigger_frequency
        self._trigger = trigger
        self._trigger_min = trigger_frequency_min
        self._trigger_scaling_factor = trigger_scaling_factor

        assert (
            self._trigger_scaling_factor > 0
        ), "The scaling factor must be higher than 0."

        self._organic_queue = organic_queue

        if self._organic_queue is None:
            self._organic_queue = OrganicQueue()

        self._step_counter = 0
        self._step_lock = asyncio.Lock()

        # Bittensor's internal checks require synapse to be a subclass of bt.Synapse.
        # If the methods are not overridden in the derived class, None is passed.
        self._axon.attach(
            forward_fn=self._on_organic_entry,
            blacklist_fn=(
                self._blacklist_fn if is_overridden(self._blacklist_fn) else None
            ),
            priority_fn=self._priority_fn if is_overridden(self._priority_fn) else None,
            verify_fn=self._verify_fn if is_overridden(self._verify_fn) else None,
        )

    def increment_step(self):
        """Increment the step counter if the trigger is set to `steps`."""
        with self._step_lock:
            if self._trigger == "steps":
                self._step_counter += 1

    def set_step(self, step: int):
        """Set the step counter to a specific value.

        Args:
            step: The step value to set.
        """
        with self._step_lock:
            if self._trigger == "steps":
                self._step_counter = step

    @abstractmethod
    async def _on_organic_entry(self, synapse: bt.Synapse) -> bt.Synapse:
        """Handle an organic entry.

        Important: this method must add the required values to the `_organic_queue`.

        Args:
            synapse: The synapse to handle.

        Returns:
            bt.StreamingSynapse: The handled synapse.
        """
        raise NotImplementedError

    async def _priority_fn(self, synapse: bt.Synapse) -> float:
        """Priority function to sort the organic queue."""
        return 0.0

    async def _blacklist_fn(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        """Blacklist function for organic handles."""
        return False, ""

    async def _verify_fn(self, synapse: bt.Synapse) -> bool:
        """Function to verify requests for organic handles."""
        return True

    async def start_loop(self):
        """The main loop for running the organic scoring task, either based on a time interval or steps.
        Calls the `sample` method to establish the sampling logic for the organic scoring task.
        """
        while not self._should_exit:
            if self._trigger == "steps":
                while self._step_counter < self._trigger_frequency:
                    await asyncio.sleep(0.1)

            try:
                logs = await self.forward()

                total_elapsed_time = logs.get("total_elapsed_time", 0)
                await self.wait_until_next(timer_elapsed=total_elapsed_time)

            except Exception as e:
                bt.logging.error(
                    f"Error occured during organic scoring iteration:\n{e}"
                )
                await asyncio.sleep(1)

    @abstractmethod
    async def forward(self) -> dict[str, Any]:
        """
        Method to establish the sampling logic for the organic scoring task.
        Sample data from the organic queue or the synthetic dataset (if available).

        Expected to return a dictionary with information from the sampling method.
        If the trigger is based on seconds, the dictionary should contain the key "total_elapsed_time".
        """
        ...

    async def wait_until_next(self, timer_elapsed: float = 0):
        """Wait until next iteration dynamically based on the size of the organic queue and the elapsed time.

        This method implements an annealing sampling rate that adapts to the growth of the organic queue,
        ensuring the system can keep up with the data processing demands.

        Args:
            timer_elapsed: The time elapsed during the current iteration of the processing loop. This is used
                to calculate the remaining sleep duration when the trigger is based on seconds.

        Behavior:
            - If the trigger is set to "seconds", the method calculates a dynamic frequency based on the current queue
            size and the scaling factor, then sleeps for the remaining duration after considering the elapsed time.
            - If the trigger is set to "steps", the method adjusts the step counter dynamically based on the current
            queue size and the scaling factor, ensuring that the system can keep up with the processing demands.

        Dynamic Adjustment:
            - The `dynamic_frequency` is calculated by reducing the original frequency by a value proportional to the
            queue size divided by the scaling factor. It ensures the frequency does not drop below `min_seconds`.
            - The `dynamic_steps` is calculated similarly, reducing the original step count by a value proportional
            to the queue size divided by the scaling factor. It ensures the steps do not drop below `min_steps`.
        """
        # Annealing sampling rate logic.
        dynamic_unit = self.sample_rate_dynamic()
        if self._trigger == "seconds":
            # Adjust the sleep duration based on the queue size.
            sleep_duration = max(dynamic_unit - timer_elapsed, 0)
            await asyncio.sleep(sleep_duration)
        elif self._trigger == "steps":
            # Adjust the steps based on the queue size.
            while True:
                if self._step_counter >= dynamic_unit:
                    self._step_counter -= dynamic_unit
                else:
                    await asyncio.sleep(1)

    def sample_rate_dynamic(self) -> float:
        """Returns dynamic sampling rate based on the size of the organic queue."""
        size = self._organic_queue.size
        delay = max(
            self._trigger_frequency - (size / self._trigger_scaling_factor),
            self._trigger_min,
        )
        return delay if self._trigger == "seconds" else int(delay)

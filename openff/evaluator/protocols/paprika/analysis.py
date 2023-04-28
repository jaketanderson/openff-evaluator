import os

import numpy as np
from openff.units import unit

from openff.evaluator.attributes import UNDEFINED
from openff.evaluator.forcefield import (
    ParameterGradient,
    SmirnoffForceFieldSource,
    TLeapForceFieldSource,
)
from openff.evaluator.forcefield.system import ParameterizedSystem
from openff.evaluator.protocols.openmm import _compute_gradients
from openff.evaluator.protocols.paprika.restraints import ApplyRestraints
from openff.evaluator.thermodynamics import ThermodynamicState
from openff.evaluator.utils.observables import (
    Observable,
    ObservableArray,
    ObservableFrame,
    ObservableType,
)
from openff.evaluator.workflow import Protocol, workflow_protocol
from openff.evaluator.workflow.attributes import InputAttribute, OutputAttribute


@workflow_protocol()
class AnalyzeAPRPhase(Protocol):
    """A protocol which will analyze the outputs of the attach, pull or release
    phases of an APR calculation and return the change in free energy for that
    phase of the calculation.
    """

    topology_path = InputAttribute(
        docstring="The file path to a coordinate file which contains topological "
        "information about the system.",
        type_hint=str,
        default_value=UNDEFINED,
    )
    trajectory_paths = InputAttribute(
        docstring="A list of paths to the trajectories (in the correct order) "
        "generated during the phase being analyzed.",
        type_hint=list,
        default_value=UNDEFINED,
    )

    phase = InputAttribute(
        docstring="The phase of the calculation being analyzed.",
        type_hint=str,
        default_value=UNDEFINED,
    )

    restraints_path = InputAttribute(
        docstring="The file path to the JSON file which contains the restraint "
        "definitions. This will usually have been generated by a "
        "`GenerateXXXRestraints` protocol.",
        type_hint=str,
        default_value=UNDEFINED,
    )

    result = OutputAttribute(
        docstring="The analysed free energy.", type_hint=Observable
    )

    def _execute(self, directory, available_resources):
        from paprika.evaluator import Analyze

        # Set-up the expected directory structure.
        windows_directory = os.path.join(directory, "windows")
        os.makedirs(windows_directory, exist_ok=True)

        window_phase = {"attach": "a", "pull": "p", "release": "r"}[self.phase]

        for window_index, trajectory_path in enumerate(self.trajectory_paths):
            # Create a directory to link the trajectory into.
            window_directory = f"{window_phase}{str(window_index).zfill(3)}"
            os.makedirs(
                os.path.join(windows_directory, window_directory), exist_ok=True
            )

            # Sym-link the trajectory into the new directory to avoid copying
            # large trajectory files.
            destination_path = os.path.join(
                windows_directory, window_directory, "trajectory.dcd"
            )
            if not os.path.isfile(destination_path):
                os.symlink(os.path.join(os.getcwd(), trajectory_path), destination_path)

            # Also sym-link the topology path
            destination_path = os.path.join(
                windows_directory, window_directory, "topology.pdb"
            )
            if not os.path.isfile(destination_path):
                os.symlink(
                    os.path.join(os.getcwd(), self.topology_path), destination_path
                )

        restraints = ApplyRestraints.load_restraints(self.restraints_path)

        flat_restraints = [
            restraint
            for restraint_type in restraints
            for restraint in restraints[restraint_type]
        ]

        results = Analyze.compute_phase_free_energy(
            phase=self.phase,
            restraints=flat_restraints,
            windows_directory=windows_directory,
            topology_name="topology.pdb",
            analysis_method="ti-block",
        )

        multiplier = {"attach": -1.0, "pull": -1.0, "release": 1.0}[self.phase]

        self.result = Observable(
            unit.Measurement(
                multiplier * results[self.phase]["ti-block"]["fe"],
                results[self.phase]["ti-block"]["sem"],
            )
        )


@workflow_protocol()
class ComputePotentialEnergyGradient(Protocol):
    """A protocol to calculate the gradient of the potential energy with
    respect to a force field parameter(s) -> <dU/dθ>.
    """

    input_system = InputAttribute(
        docstring="The file path to the force field parameters to assign to the system.",
        type_hint=ParameterizedSystem,
        default_value=UNDEFINED,
    )
    topology_path = InputAttribute(
        docstring="The path to the topology file to compute the gradient.",
        type_hint=str,
        default_value=UNDEFINED,
    )
    trajectory_path = InputAttribute(
        docstring="The path to the trajectory file to compute the gradient.",
        type_hint=str,
        default_value=UNDEFINED,
    )
    thermodynamic_state = InputAttribute(
        docstring="The thermodynamic state that the calculation was performed at.",
        type_hint=ThermodynamicState,
        default_value=UNDEFINED,
    )
    enable_pbc = InputAttribute(
        docstring="Whether PBC should be enabled when re-evaluating system energies.",
        type_hint=bool,
        default_value=True,
    )
    gradient_parameters = InputAttribute(
        docstring="An optional list of parameters to differentiate the estimated "
        "free energy with respect to.",
        type_hint=list,
        default_value=lambda: list(),
    )
    potential_energy_gradients = OutputAttribute(
        docstring="A list of the gradient of the potential energy w.r.t. to FF parameters.",
        type_hint=list,
    )
    potential_energy_gradients_data = OutputAttribute(
        docstring="The time series data of the gradient of the potential w.r.t to FF parameters.",
        type_hint=list,
    )

    def _execute(self, directory, available_resources):
        import mdtraj
        from simtk.openmm.app import Modeller, PDBFile

        # Set-up the expected directory structure.
        windows_directory = os.path.join(directory, "window")
        os.makedirs(windows_directory, exist_ok=True)

        destination_topology_path = os.path.join(windows_directory, "topology.pdb")
        destination_trajectory_path = os.path.join(windows_directory, "trajectory.dcd")

        # Work around because dummy atoms are not support in OFF.
        # Write a new PDB file without dummy atoms.
        coords = PDBFile(self.topology_path)
        new_topology = Modeller(coords.topology, coords.positions)
        dummy_atoms = [r for r in new_topology.getTopology().atoms() if r.name == "DUM"]
        new_topology.delete(dummy_atoms)

        with open(destination_topology_path, "w") as file:
            PDBFile.writeFile(
                new_topology.getTopology(),
                new_topology.getPositions(),
                file,
                keepIds=True,
            )

        # Write a new DCD file without dummy atoms.
        trajectory = mdtraj.load_dcd(
            os.path.join(os.getcwd(), self.trajectory_path),
            top=os.path.join(os.getcwd(), self.topology_path),
        )
        trajectory.atom_slice(
            [i for i in range(trajectory.n_atoms - len(dummy_atoms))]
        ).save_dcd(destination_trajectory_path)

        # Load in the new trajectory
        trajectory = mdtraj.load_dcd(
            destination_trajectory_path,
            top=destination_topology_path,
        )

        # Placeholder for gradient
        observables = ObservableFrame(
            {
                ObservableType.PotentialEnergy: ObservableArray(
                    value=np.zeros((len(trajectory), 1)) * unit.kilojoule / unit.mole
                )
            }
        )

        # Compute the gradient in the first solvent.
        force_field_source = self.input_system.force_field
        force_field = (
            force_field_source.to_force_field()
            if isinstance(force_field_source, SmirnoffForceFieldSource)
            else force_field_source
        )
        gaff_system_path = None
        gaff_topology_path = None
        if isinstance(force_field_source, TLeapForceFieldSource):
            gaff_system_path = self.input_system.system_path
            gaff_topology_path = self.input_system.system_path.replace("xml", "prmtop")

        _compute_gradients(
            self.gradient_parameters,
            observables,
            force_field,
            self.thermodynamic_state,
            self.input_system.topology,
            trajectory,
            available_resources,
            gaff_system_path,
            gaff_topology_path,
            self.enable_pbc,
        )

        self.potential_energy_gradients = [
            ParameterGradient(key=gradient.key, value=gradient.value.mean().item())
            for gradient in observables[ObservableType.PotentialEnergy].gradients
        ]
        self.potential_energy_gradients_data = [
            ParameterGradient(key=gradient.key, value=gradient.value)
            for gradient in observables[ObservableType.PotentialEnergy].gradients
        ]


@workflow_protocol()
class ComputeFreeEnergyGradient(Protocol):
    """A protocol to calculate the free-energy gradient of the binding free energy
    respect to a force field parameter(s). d(ΔG°)/dθ = <dU/dθ>_bound - <dU/dθ>_unbound
    """

    bound_state_gradients = InputAttribute(
        docstring="The gradient of the potential energy for the bound state.",
        type_hint=list,
        default_value=UNDEFINED,
    )
    unbound_state_gradients = InputAttribute(
        docstring="The gradient of the potential energy for the unbound state.",
        type_hint=list,
        default_value=UNDEFINED,
    )
    orientation_free_energy = InputAttribute(
        docstring="The total free energy for a particular binding orientation.",
        type_hint=Observable,
        default_value=UNDEFINED,
    )
    result = OutputAttribute(
        docstring="The free energy with the gradients stored as Observable.",
        type_hint=Observable,
    )

    def _execute(self, directory, available_resources):
        bound_state = {
            gradient.key: gradient for gradient in self.bound_state_gradients[0]
        }
        unbound_state = {
            gradient.key: gradient for gradient in self.unbound_state_gradients[0]
        }

        free_energy_gradients = [
            bound_state[key] - unbound_state[key] for key in bound_state
        ]

        self.result = Observable(
            self.orientation_free_energy.value.plus_minus(
                self.orientation_free_energy.error
            ),
            gradients=free_energy_gradients,
        )


@workflow_protocol()
class ComputeSymmetryCorrection(Protocol):
    """Computes the symmetry correction for an APR calculation which involves
    a guest with symmetry.
    """

    n_microstates = InputAttribute(
        docstring="The number of symmetry microstates of the guest molecule.",
        type_hint=int,
        default_value=UNDEFINED,
    )
    thermodynamic_state = InputAttribute(
        docstring="The thermodynamic state that the calculation was performed at.",
        type_hint=ThermodynamicState,
        default_value=UNDEFINED,
    )

    result = OutputAttribute(docstring="The symmetry correction.", type_hint=Observable)

    def _execute(self, directory, available_resources):
        from paprika.evaluator import Analyze

        self.result = Observable(
            unit.Measurement(
                Analyze.symmetry_correction(
                    self.n_microstates,
                    self.thermodynamic_state.temperature.to(unit.kelvin).magnitude,
                ),
                0 * unit.kilocalorie / unit.mole,
            )
        )


@workflow_protocol()
class ComputeReferenceWork(Protocol):
    """Computes the reference state work."""

    thermodynamic_state = InputAttribute(
        docstring="The thermodynamic state that the calculation was performed at.",
        type_hint=ThermodynamicState,
        default_value=UNDEFINED,
    )

    restraints_path = InputAttribute(
        docstring="The file path to the JSON file which contains the restraint "
        "definitions. This will usually have been generated by a "
        "`GenerateXXXRestraints` protocol.",
        type_hint=str,
        default_value=UNDEFINED,
    )

    result = OutputAttribute(
        docstring="The reference state work.", type_hint=Observable
    )

    def _execute(self, directory, available_resources):
        from paprika.evaluator import Analyze

        restraints = ApplyRestraints.load_restraints(self.restraints_path)
        guest_restraints = restraints["guest"]

        self.result = Observable(
            unit.Measurement(
                -Analyze.compute_ref_state_work(
                    self.thermodynamic_state.temperature.to(unit.kelvin).magnitude,
                    guest_restraints,
                ),
                0 * unit.kilocalorie / unit.mole,
            )
        )

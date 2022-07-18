import os
import shutil
import numpy as np
import tempfile as tf
from tqdm.auto import tqdm
import multiprocessing as mp
from functools import partial
from typing import List, Dict, Union, NoReturn
import logging

from moleculekit.molecule import Molecule
from ffevaluation.ffevaluate import FFEvaluate
from pocketoptimizer.sampling.sidechain_rotamers_ffev import FFRotamerSampler
from pocketoptimizer.utility.utils import DotDict

logging.root.handlers = []
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get('POCKETOPTIMIZER_LOGFILE')),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('pocketoptimizer.sampling.peptide_sampler')

class PeptideSampler(FFRotamerSampler):
    """ Class for peptide conformer sampling based on rotamers using either
    backbone independent rotamers from cmlib or backbone dependent rotamers from dunbrack rotamer library.
    cmlib:
        * superimpose rotamers onto structure
    dunbrack:
        * read rotamers for respective phi/psi angle combination
        * get all chi angles for aa
        * convert to radians
        * set dihedral angles
        * prune based on probability
    - save conformers into pdb file
    """
    def __init__(self, work_dir: str, positions: List[Dict[str, Union[str, str]]], forcefield: str,
                 params_folder: str = '', library: str = 'dunbrack', tmp: str = '/tmp/'):
        """
        Constructor method.

        Parameters
        ----------
        work_dir: str
            Path to working directory
        positions: list
            Dictionary containing chain and residue identifiers for the residues where rotamers should be sampled
        forcefield: str
            Force field to use for energy calculations
        params_folder: str
            Path to peptide parameters folder [default: '']
        library: str
            Library to use for selecting rotamers, either setting coordinates from pdb after superimposing (cmlib)
            or setting dihedral angles from dunbrack [default: 'dunbrack']
        tmp: str
            Temporary directory to write temporary files [default: '/tmp']
        """
        self.work_dir = work_dir
        self.positions = positions
        self.library = library
        self.forcefield = forcefield
        self.params = params_folder
        self.tmp = tmp

    def merge_conformers(self, conf_ids: List[int], outfile: str) -> NoReturn:
            """
            Writes out rotamers as models into one merged .pdb file

            Parameters
            ----------
            conf_ids: int
                Conformers with an energy below the threshold
            outfile: name/path of the output file
            """
            with open(outfile, 'w') as merged_rot_file:
                for conf_id in conf_ids:
                    with open(os.path.join(self.tmp, f'ligand_conf_{conf_id}.pdb'), 'r') as conf_file:
                        for line in conf_file:
                            if line.startswith('MODEL'):
                                line = f'MODEL        {str(conf_id)}\n'
                            # skip 'END' lines (distinguish between 'END' and 'ENDMDL')
                            elif line.startswith('END') and not line.startswith('ENDMDL'):
                                continue
                            merged_rot_file.write(line)
                merged_rot_file.write('END')

    def calculate_vdw(self, conf_id: int, structure: Molecule, ffev: FFEvaluate) -> np.float:
        """
        Calculates the energy of a peptide conformation

        Parameters
        ----------
        conf_id: int
            Conformer id to calculate
        structure: class:moleculekit.Molecule
            Object containing conformers
        ffev: :class:ffevaluation.ffevaluate.FFevaluate
            Force field object

        Returns
        -------
        Returns summed energie
        """
        # Set coordinates to coordinates of conformer
        structure.set('coords', structure.coords[:, :, conf_id])
        energies = ffev.calculateEnergies(structure.coords[:, :, conf_id])
        structure.write(os.path.join(self.tmp, f'ligand_conf_{conf_id}.pdb'))
        total_nrg = 0
        for comp, nrg in energies.items():
            if comp == 'elec':
                total_nrg += nrg * 0.01
            elif comp == 'total':
                continue
            else:
                total_nrg += nrg

        return total_nrg

    def conformer_sampling(self, nrg_thresh: float = 100.0, dunbrack_filter_thresh: float = -1,
                           expand: List[str] = [], ncpus: int = 1, _keep_tmp: bool = False) -> NoReturn:
        """
        Parameters
        ----------
        nrg_thresh: float
            Filtering threshold to avoid clashes. [default: 100 kcal/mol]
        dunbrack_filter_thresh: float
            Filter threshold, rotamers having probability of occurence lower than filter threshold will
            be pruned if their rotameric mode does occur more than once
            (-1: no pruning, 1: pruning of all rotamers with duplicate rotamer modes) [default: -1]
        expand: list
            List of chi angles to expand [default: ['chi1', 'chi2']]
        ncpus: int
            Nubmer of CPUs to use for multiprocessing [default: 1]
        _keep_tmp: bool
            If the tmp directory should be deleted or not. Useful for debugging. [default: False]
        """
        from pocketoptimizer.utility.utils import MutationProcessor, load_ff_parameters, calculate_chunks

        if self.forcefield == 'amber_ff14SB':
            from pocketoptimizer.utility.molecule_types import _SIDECHAIN_TORSIONS_AMBER as _SIDECHAIN_TORSIONS
        elif self.forcefield == 'charmm36':
            from pocketoptimizer.utility.molecule_types import _SIDECHAIN_TORSIONS_CHARMM as _SIDECHAIN_TORSIONS
        else:
            logger.error('Force field not implemented.')
            raise NotImplementedError('Force field not implemented.')

        outfile = os.path.join(self.work_dir, 'ligand', self.forcefield, 'conformers', 'ligand_confs.pdb')

        if os.path.isfile(outfile):
            logger.info('Conformers are already sampled.')
            return

        self.tmp = tf.mkdtemp(dir=self.tmp, prefix='calculateRotamers_')
        os.chdir(self.tmp)

        logger.info('Start conformer sampling procedure.')
        structure_path = os.path.join(self.work_dir, 'ligand', self.forcefield, 'ligand.pdb')
        struc = Molecule(structure_path)
        confs = struc.copy()

        mutation_processor = MutationProcessor(structure=structure_path, mutations=self.positions)
        termini_positions = mutation_processor.check_termini()

        for position in self.positions:
            chain = position['chain']
            resid = position['resid']
            resname = struc.get('resname', f'chain {chain} and resid {resid} and name CA')[0]
            N_terminus = False
            C_terminus = False

            # Check if the position is at the N-or C-terminus of the protein
            if 'N-terminus' in termini_positions:
                if f'{chain}_{resid}' in termini_positions['N-terminus']:
                    N_terminus = True
            elif 'C-terminus' in termini_positions:
                if f'{chain}_{resid}' in termini_positions['C-terminus']:
                    C_terminus = True

            if self.library == 'cmlib':
                if resname == 'GLY' or resname == 'ALA':
                    residue = struc.copy()
                    residue.filter(f'chain {chain} and resid {resid}', _logger=False)
                else:
                    residue = self.read_cmlib(resname)
                    native_residue = struc.copy()

                    # Take care of additional atoms at N-and C-terminus
                    if N_terminus:
                        native_residue.filter(f'chain {chain} and resid {resid} and not (name H2 or name H3)', _logger=False)
                    elif C_terminus:
                        native_residue.filter(f'chain {chain} and resid {resid} and not name OXT', _logger=False)
                    else:
                        native_residue.filter(f'chain {chain} and resid {resid}', _logger=False)
                    native_conf = np.expand_dims(native_residue.get('coords', sel=f'chain {chain} and resid {resid}'), axis=2)
                    residue.coords = np.dstack((residue.coords, native_conf))
                        # Set rotamers backbone onto backbone of residue
                    ref = f'(name CA or name C or name N) and resid {resid} and chain {chain}'
                    residue.align(sel='name N or name C or name CA', refmol=struc, refsel=ref)

            elif self.library == 'dunbrack':
                residue = struc.copy()
                residue.filter(f'chain {chain} and resid {resid}', _logger=False)

                if resname != 'GLY' and resname != 'ALA':
                    if not N_terminus:
                        phi_indices = struc.get('index', sel=f'(chain {chain} and resid {str(int(resid)-1)} and name C) or (chain {chain} and resid {resid} and (name N or name CA or name C))')
                        phi_angle = struc.getDihedral(phi_indices) * (180/np.pi) + 180
                    if not C_terminus:
                        psi_indices = struc.get('index', sel=f'(chain {chain} and resid {resid} and (name N or name CA or name C)) or (chain {chain} and resid {str(int(resid)+1)} and name N)')
                        psi_angle = struc.getDihedral(psi_indices) * (180/np.pi) + 180

                    # Read histidine rotamers for different HIS protonation states
                    if resname in ['HID', 'HIE', 'HIP']:
                        resname = 'HIS'

                    rotamers = self.read_dunbrack(resname=resname,
                                                  phi_angle=phi_angle,
                                                  psi_angle=psi_angle,
                                                  N_terminus=N_terminus,
                                                  C_terminus=C_terminus,
                                                  prob_cutoff=dunbrack_filter_thresh)

                    chi_angles = self.expand_dunbrack(rotamers=rotamers,
                                                      expand=expand)

                    current_rot = residue.copy()
                    # Break proline rings, since no dihedral setting possible
                    if resname == 'PRO':
                        current_rot.deleteBonds(sel='name N or name CD', inter=False)
                    bonds = current_rot.bonds
                    # Iterate over all rotamers
                    for rotamer in chi_angles:
                        for i, torsion in enumerate(_SIDECHAIN_TORSIONS[resname]):
                            # select the four atoms forming the dihedral angle according to their atom names
                            index_1 = int(current_rot.get('index', sel=f'name {torsion[0]}'))
                            index_2 = int(current_rot.get('index', sel=f'name {torsion[1]}'))
                            index_3 = int(current_rot.get('index', sel=f'name {torsion[2]}'))
                            index_4 = int(current_rot.get('index', sel=f'name {torsion[3]}'))
                            current_rot.setDihedral([index_1, index_2, index_3, index_4], rotamer[i] * (np.pi/180), bonds=bonds)
                        # append rotameric states as frames to residue
                        residue.appendFrames(current_rot)
                residue.dropFrames(drop=0)

            else:
                logger.error(f'Library: {self.library} not a valid option. Try cmlib or dunbrack.')
                raise ValueError(f'Library: {self.library} not a valid option. Try cmlib or dunbrack.')

            for conf_id in range(confs.coords.shape[-1]):
                modified_conf = struc.copy()
                modified_conf.set('coords', confs.coords[:, :, conf_id])
                for rotamer_id in range(residue.coords.shape[-1]):
                    modified_conf.set('coords', residue.coords[:, :, rotamer_id], sel=f'chain {chain} and resid {resid}')
                    confs.appendFrames(modified_conf)

        nconfs = confs.coords.shape[-1]

        struc, prm = load_ff_parameters(structure_path=self.params,
                                        forcefield=self.forcefield)

        # Generate FFEvaluate object
        ffev = FFEvaluate(struc, prm)

        energies = np.ndarray(nconfs)
        with tqdm(total=nconfs, desc='Filter Conformers') as pbar:
            with mp.Pool(processes=ncpus) as pool:
                for pose_id, energy in enumerate(pool.imap(
                        partial(self.calculate_vdw,
                                structure=confs.copy(),
                                ffev=ffev
                                ), np.arange(nconfs),
                        chunksize=calculate_chunks(nposes=nconfs, ncpus=ncpus))):
                    energies[pose_id] = energy
                    pbar.update()
        val_ids = [val_id[0] for val_id in np.argwhere(energies <= min(energies) + nrg_thresh)]

        self.merge_conformers(conf_ids=val_ids, outfile=outfile)
        logger.info(f'Generated {len(val_ids)} conformers.')

        if not _keep_tmp:
            if os.path.isdir(self.tmp):
                shutil.rmtree(self.tmp)
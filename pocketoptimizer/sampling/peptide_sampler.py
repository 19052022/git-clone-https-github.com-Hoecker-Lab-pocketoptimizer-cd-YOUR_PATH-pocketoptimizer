import os
import shutil
import numpy as np
import tempfile as tf
from tqdm.auto import tqdm
import multiprocessing as mp
from functools import partial
from typing import List, Tuple, Dict, NoReturn
import logging
from moleculekit.molecule import Molecule
from ffevaluation.ffevaluate import FFEvaluate

from pocketoptimizer.sampling.sidechain_rotamers_ffev import FFRotamerSampler

logger = logging.getLogger(__name__)


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
    def __init__(self, library: str, positions: List[Dict[str, str]], **kwargs):
        """Constructor method

        Parameters
        ---------
        library: str
            Rotamer library to use

        """
        super().__init__(**kwargs)
        self.library = library
        self.positions = positions
        # Chi-angles for phosphorylated amino acids from PDB taken from Rosetta patches
        self.extra_chi_ptms = {'SEP': {'chi': [-179.16, 152.48, 117.55, -106.2, 153.19],
                                       'std': [10.17, 4.57, 15.89, 15.99, 7.19]},
                               'PTR': {'chi': [-83.79, -176.68, -0.47, -131.33, 61.23, 100.14],
                                       'std': [17.19, 12.24, 20.06, 9.85, 12.12, 13.39]}
                               }

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
                    with open(os.path.join(self.tmp_dir, f'ligand_conf_{conf_id}.pdb'), 'r') as conf_file:
                        for line in conf_file:
                            if line.startswith('MODEL'):
                                line = f'MODEL        {str(conf_id)}\n'
                            # skip 'END' lines (distinguish between 'END' and 'ENDMDL')
                            elif line.startswith('END') and not line.startswith('ENDMDL'):
                                continue
                            merged_rot_file.write(line)
                merged_rot_file.write('END')

    def calculate_energy(self, conf_id: int, structure: Molecule, ffev: FFEvaluate) -> np.float:
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
        Returns vdw energy
        """
        # Set coordinates to coordinates of conformer
        structure.set('coords', structure.coords[:, :, conf_id])
        energies = ffev.calculateEnergies(structure.coords[:, :, conf_id])
        structure.write(os.path.join(self.tmp_dir, f'ligand_conf_{conf_id}.pdb'))

        return energies['vdw']

    def expand_ptms(self, resname: str, rotamers: Dict[str, List[Tuple[float]]]) -> Dict[str, List[Tuple[float]]]:
        """
        Samples additional rotations for phosphate groups

        Parameters
        ----------
        resname: str
            Resname of the Residue to expand
        rotamers: dict
            Dictionaries for rotamers containing chi angles and standard deviations

        Returns
        -------
        Dictionary with lists of Chi-angle tuples
        """
        num_chi = len(self.sidechain_torsions[resname]) - 1
        rotamers_expand = {'chi': [], 'std': []}

        for i, rotamer in enumerate(rotamers['chi']):
            for j, chi in enumerate(self.extra_chi_ptms[resname]['chi']):
                _chis = list(rotamer)
                _stds = list(rotamers['std'][i])
                _chis[num_chi] = chi
                _stds[num_chi] = self.extra_chi_ptms[resname]['std'][j]
                rotamers_expand['chi'].append(tuple(_chis))
                rotamers_expand['std'].append(tuple(_stds))
        return rotamers_expand

    def conformer_sampling(self, nrg_thresh: float = 100.0, dunbrack_prob: float = -1,
                           expand: List[str] = [], accurate: bool = False, _keep_tmp: bool = False) -> NoReturn:
        """
        Parameters
        ----------
        nrg_thresh: float
            Filtering threshold to avoid clashes. [default: 100 kcal/mol]
        dunbrack_prob: float
            Filter threshold, rotamers having probability of occurence lower than filter threshold will
            be pruned if their rotameric mode does occur more than once
            (-1: no pruning, 1: pruning of all rotamers with duplicate rotamer modes) [default: -1]
        expand: list
            List of chi angles to expand [default: ['chi1', 'chi2']]
        accurate: bool
            Whether to expand chi-angles by +/-1 std or also 0.5 std [default: False]
        _keep_tmp: bool
            If the tmp directory should be deleted or not. Useful for debugging. [default: False]
        """
        from pocketoptimizer.utility.utils import MutationProcessor, load_ff_parameters, calculate_chunks

        outfile = os.path.join(self.work_dir, 'ligand', self.forcefield, 'conformers', 'ligand_confs.pdb')

        if os.path.isfile(outfile):
            logger.info('Conformers are already sampled.')
            return

        self.tmp_dir = tf.mkdtemp(dir=self.tmp_dir, prefix='calculateRotamers_')
        os.chdir(self.tmp_dir)

        logger.info('Start conformer sampling procedure.')

        mutation_processor = MutationProcessor(structure=self.ligand_protonated,
                                               mutations=self.positions,
                                               forcefield=self.forcefield)
        termini_positions = mutation_processor.check_termini()
        struc, prm = load_ff_parameters(structure_path=self.built_ligand_params['params_folder'],
                                        forcefield=self.forcefield)
        confs = struc.copy()

        for position in self.positions:

            chain = position['chain']
            resid = position['resid']
            resname = struc.get('resname', f'chain {chain} and resid {resid} and name CA')[0]

            # Keep original rotamer
            residue = struc.copy()
            residue.filter(f'chain {chain} and resid {resid}', _logger=False)
            if resname != 'GLY' and resname != 'ALA':
                if self.library == 'cmlib':
                    rotamers = self.read_db(resname=resname)
                elif self.library == 'dunbrack':
                    # Check if the position is at the N-or C-terminus of the protein
                    if f'{chain}_{resid}' in termini_positions:
                        self.library = 'cmlib'
                        rotamers = self.read_db(resname=resname)
                        self.library = 'dunbrack'
                    else:
                        phi_angle = struc.getDihedral([int(struc.get('index', sel=f'chain {chain} and resid {str(int(resid)-1)} and name C')),
                                                       int(struc.get('index', sel=f'chain {chain} and resid {resid} and name N')),
                                                       int(struc.get('index', sel=f'chain {chain} and resid {resid} and name CA')),
                                                       int(struc.get('index', sel=f'chain {chain} and resid {resid} and name C'))
                                                       ]) * (180/np.pi) + 180

                        psi_angle = struc.getDihedral([int(struc.get('index', sel=f'chain {chain} and resid {resid} and name N')),
                                                       int(struc.get('index', sel=f'chain {chain} and resid {resid} and name CA')),
                                                       int(struc.get('index', sel=f'chain {chain} and resid {resid} and name C')),
                                                       int(struc.get('index', sel=f'chain {chain} and resid {str(int(resid)+1)} and name N'))
                                                       ]) * (180/np.pi) + 180

                        # Read histidine rotamers for different HIS protonation states
                        if resname in ['HID', 'HIE', 'HIP', 'HSD', 'HSE', 'HSP']:
                            _resname = 'HIS'
                        elif resname == 'SEP':
                            _resname = 'SER'
                        elif resname == 'PTR':
                            _resname = 'TYR'
                        else:
                            _resname = resname

                        rotamers = self.read_db(resname=_resname,
                                                phi_angle=phi_angle,
                                                psi_angle=psi_angle,
                                                prob_cutoff=dunbrack_prob)

                        if resname in ['SEP', 'PTR']:
                            rotamers = self.expand_ptms(resname=resname,
                                                        rotamers=rotamers)

                        rotamers = self.expand_dunbrack(rotamers=rotamers,
                                                        expand=expand,
                                                        accurate=accurate)

                else:
                    logger.error(f'Library: {self.library} not implemented.')
                    raise ValueError(f'Library: {self.library} not implemented.')

                # Keep original rotamer
                current_rot = residue.copy()
                # Break proline rings, since no dihedral setting possible
                if resname == 'PRO':
                    current_rot.deleteBonds(sel='name N or name CD', inter=False)
                bonds = current_rot.bonds
                # Iterate over all rotamers
                for rotamer in rotamers['chi']:
                    for i, torsion in enumerate(self.sidechain_torsions[resname]):
                        # select the four atoms forming the dihedral angle according to their atom names
                        current_rot.setDihedral([int(current_rot.get('index', sel=f'name {torsion[0]}')),
                                                 int(current_rot.get('index', sel=f'name {torsion[1]}')),
                                                 int(current_rot.get('index', sel=f'name {torsion[2]}')),
                                                 int(current_rot.get('index', sel=f'name {torsion[3]}'))],
                                                rotamer[i] * (np.pi/180), bonds=bonds)
                    # append rotameric states as frames to residue
                    residue.appendFrames(current_rot)
                # Remove the original conformation
                residue.dropFrames(drop=0)

            num_confs = confs.coords.shape[-1]
            for conf_id in range(num_confs):
                modified_conf = struc.copy()
                modified_conf.set('coords', confs.coords[:, :, conf_id])
                for rotamer_id in range(residue.coords.shape[-1]):
                    modified_conf.set('coords', residue.coords[:, :, rotamer_id], sel=f'chain {chain} and resid {resid}')
                    confs.appendFrames(modified_conf)
            # Remove the starting conformation
            confs.dropFrames(drop=num_confs - 1)

        nconfs = confs.coords.shape[-1]

        # Generate FFEvaluate object
        ffev = FFEvaluate(struc, prm)

        energies = np.ndarray(nconfs)
        with tqdm(total=nconfs, desc='Filter Conformers') as pbar:
            with mp.Pool(processes=self.ncpus) as pool:
                for conf_id, energy in enumerate(pool.imap(
                        partial(self.calculate_energy,
                                structure=confs.copy(),
                                ffev=ffev
                                ), np.arange(nconfs),
                        chunksize=calculate_chunks(nposes=nconfs, ncpus=self.ncpus))):
                    energies[conf_id] = energy
                    pbar.update()
        val_ids = [val_id[0] for val_id in np.argwhere(energies <= min(energies) + nrg_thresh)]

        self.merge_conformers(conf_ids=val_ids, outfile=outfile)
        logger.info(f'Generated {len(val_ids)} conformers.')

        if not _keep_tmp:
            if os.path.isdir(self.tmp_dir):
                shutil.rmtree(self.tmp_dir)
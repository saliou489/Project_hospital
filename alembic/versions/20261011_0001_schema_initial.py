"""Schéma initial de MédiRDV (patients, médecins, agents, rendez-vous, comptes, pointages, journal).

Les contraintes des types énumérés sont créées par sa.Enum(create_constraint=True) : elles ne sont pas répétées.
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.String(length=10), nullable=False),
    sa.Column('actif', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('journal_actions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('horodatage', sa.DateTime(), nullable=False),
    sa.Column('utilisateur_id', sa.String(length=12), nullable=True),
    sa.Column('role', sa.String(length=30), nullable=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('cible', sa.String(length=30), nullable=True),
    sa.Column('detail', sa.String(length=300), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_journal_actions_action'), 'journal_actions', ['action'], unique=False)
    op.create_index(op.f('ix_journal_actions_horodatage'), 'journal_actions', ['horodatage'], unique=False)
    op.create_index(op.f('ix_journal_actions_utilisateur_id'), 'journal_actions', ['utilisateur_id'], unique=False)
    op.create_table('medecins',
    sa.Column('id', sa.String(length=10), nullable=False),
    sa.Column('nom', sa.String(length=120), nullable=False),
    sa.Column('specialite', sa.String(length=60), nullable=False),
    sa.Column('duree_consultation_min', sa.Integer(), nullable=False),
    sa.Column('actif', sa.Boolean(), nullable=False),
    sa.CheckConstraint('duree_consultation_min IN (15, 20, 30)', name='ck_medecin_duree'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_medecins_specialite'), 'medecins', ['specialite'], unique=False)
    op.create_table('patients',
    sa.Column('id', sa.String(length=12), nullable=False),
    sa.Column('nom', sa.String(length=80), nullable=False),
    sa.Column('prenom', sa.String(length=80), nullable=False),
    sa.Column('sexe', sa.Enum('F', 'M', name='sexe_patient', native_enum=False, create_constraint=True, length=30), nullable=False),
    sa.Column('date_naissance', sa.Date(), nullable=True),
    sa.Column('telephone', sa.String(length=9), nullable=True),
    sa.Column('quartier', sa.String(length=60), nullable=True),
    sa.Column('cree_le', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('rendez_vous',
    sa.Column('id', sa.String(length=12), nullable=False),
    sa.Column('patient_id', sa.String(length=12), nullable=False),
    sa.Column('medecin_id', sa.String(length=10), nullable=False),
    sa.Column('agent_id', sa.String(length=10), nullable=False),
    sa.Column('date_prise', sa.Date(), nullable=False),
    sa.Column('debut', sa.DateTime(), nullable=False),
    sa.Column('fin', sa.DateTime(), nullable=False),
    sa.Column('statut', sa.Enum('Planifié', 'Confirmé', 'Honoré', 'Absent', 'Annulé', name='statut_rdv', native_enum=False, create_constraint=True, length=30), nullable=False),
    sa.Column('confirme', sa.Boolean(), nullable=True),
    sa.Column('canal', sa.Enum('Téléphone', 'Guichet', 'Recommandation médecin', name='canal_rdv', native_enum=False, create_constraint=True, length=30), nullable=True),
    sa.Column('motif_annulation', sa.Enum('Patient', 'Médecin', 'Clinique', 'Autre', name='motif_annulation', native_enum=False, create_constraint=True, length=30), nullable=True),
    sa.Column('cree_le', sa.DateTime(), nullable=False),
    sa.Column('modifie_le', sa.DateTime(), nullable=False),
    sa.CheckConstraint("(statut = 'Annulé' AND motif_annulation IS NOT NULL) OR (statut <> 'Annulé' AND motif_annulation IS NULL)", name='ck_rdv_motif_annulation'),
    sa.CheckConstraint('fin > debut', name='ck_rdv_fin_apres_debut'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['medecin_id'], ['medecins.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_rdv_debut_statut', 'rendez_vous', ['debut', 'statut'], unique=False)
    op.create_index(op.f('ix_rendez_vous_agent_id'), 'rendez_vous', ['agent_id'], unique=False)
    op.create_index(op.f('ix_rendez_vous_medecin_id'), 'rendez_vous', ['medecin_id'], unique=False)
    op.create_index(op.f('ix_rendez_vous_patient_id'), 'rendez_vous', ['patient_id'], unique=False)
    op.create_index('uq_rdv_medecin_debut_actif', 'rendez_vous', ['medecin_id', 'debut'], unique=True, sqlite_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"), postgresql_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"))
    op.create_index('uq_rdv_patient_debut_actif', 'rendez_vous', ['patient_id', 'debut'], unique=True, sqlite_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"), postgresql_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"))
    op.create_table('utilisateurs',
    sa.Column('id', sa.String(length=12), nullable=False),
    sa.Column('role', sa.Enum("Agent d'accueil", 'Médecin', 'Responsable des opérations', 'Direction', 'Administrateur', name='role_utilisateur', native_enum=False, create_constraint=True, length=30), nullable=False),
    sa.Column('nom_affichage', sa.String(length=120), nullable=False),
    sa.Column('cle_hash', sa.String(length=255), nullable=False),
    sa.Column('actif', sa.Boolean(), nullable=False),
    sa.Column('medecin_id', sa.String(length=10), nullable=True),
    sa.Column('agent_id', sa.String(length=10), nullable=True),
    sa.Column('essais_echoues', sa.Integer(), nullable=False),
    sa.Column('verrouille_jusqua', sa.DateTime(), nullable=True),
    sa.Column('derniere_connexion', sa.DateTime(), nullable=True),
    sa.Column('cree_le', sa.DateTime(), nullable=False),
    sa.CheckConstraint("(role = 'Agent d''accueil') = (agent_id IS NOT NULL)", name='ck_utilisateur_lien_agent'),
    sa.CheckConstraint("(role = 'Médecin') = (medecin_id IS NOT NULL)", name='ck_utilisateur_lien_medecin'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['medecin_id'], ['medecins.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_id'),
    sa.UniqueConstraint('medecin_id')
    )
    op.create_table('pointages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('utilisateur_id', sa.String(length=12), nullable=False),
    sa.Column('jour', sa.Date(), nullable=False),
    sa.Column('arrivee', sa.DateTime(), nullable=False),
    sa.Column('depart', sa.DateTime(), nullable=True),
    sa.Column('corrige_par', sa.String(length=12), nullable=True),
    sa.Column('corrige_le', sa.DateTime(), nullable=True),
    sa.CheckConstraint('depart IS NULL OR depart >= arrivee', name='ck_pointage_depart_apres_arrivee'),
    sa.ForeignKeyConstraint(['utilisateur_id'], ['utilisateurs.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('utilisateur_id', 'jour', name='uq_pointage_utilisateur_jour')
    )
    op.create_index(op.f('ix_pointages_utilisateur_id'), 'pointages', ['utilisateur_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_pointages_utilisateur_id'), table_name='pointages')
    op.drop_table('pointages')
    op.drop_table('utilisateurs')
    op.drop_index('uq_rdv_patient_debut_actif', table_name='rendez_vous', sqlite_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"), postgresql_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"))
    op.drop_index('uq_rdv_medecin_debut_actif', table_name='rendez_vous', sqlite_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"), postgresql_where=sa.text("statut IN ('Planifié', 'Confirmé', 'Honoré')"))
    op.drop_index(op.f('ix_rendez_vous_patient_id'), table_name='rendez_vous')
    op.drop_index(op.f('ix_rendez_vous_medecin_id'), table_name='rendez_vous')
    op.drop_index(op.f('ix_rendez_vous_agent_id'), table_name='rendez_vous')
    op.drop_index('ix_rdv_debut_statut', table_name='rendez_vous')
    op.drop_table('rendez_vous')
    op.drop_table('patients')
    op.drop_index(op.f('ix_medecins_specialite'), table_name='medecins')
    op.drop_table('medecins')
    op.drop_index(op.f('ix_journal_actions_utilisateur_id'), table_name='journal_actions')
    op.drop_index(op.f('ix_journal_actions_horodatage'), table_name='journal_actions')
    op.drop_index(op.f('ix_journal_actions_action'), table_name='journal_actions')
    op.drop_table('journal_actions')
    op.drop_table('agents')

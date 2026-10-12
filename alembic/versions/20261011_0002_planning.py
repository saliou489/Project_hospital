"""Planning : spécialités, plages de consultation, jours fériés, indisponibilités, historique des rendez-vous.

Révision : 0002
Révise : 0001
Créée le : 2026-10-11 18:55:15.966820
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('jours_feries',
    sa.Column('jour', sa.Date(), nullable=False),
    sa.Column('libelle', sa.String(length=120), nullable=False),
    sa.Column('type', sa.Enum('Jour férié', 'Fermeture exceptionnelle', name='type_fermeture', native_enum=False, create_constraint=True, length=30), nullable=False),
    sa.PrimaryKeyConstraint('jour')
    )
    op.create_table('specialites',
    sa.Column('nom', sa.String(length=60), nullable=False),
    sa.Column('duree_standard_min', sa.Integer(), nullable=False),
    sa.Column('couleur', sa.String(length=7), nullable=False),
    sa.Column('actif', sa.Boolean(), nullable=False),
    sa.CheckConstraint('duree_standard_min IN (15, 20, 30)', name='ck_specialite_duree'),
    sa.PrimaryKeyConstraint('nom')
    )
    op.create_table('indisponibilites',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('medecin_id', sa.String(length=10), nullable=False),
    sa.Column('debut', sa.DateTime(), nullable=False),
    sa.Column('fin', sa.DateTime(), nullable=False),
    sa.Column('motif', sa.String(length=200), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('declaree_par', sa.String(length=12), nullable=True),
    sa.Column('declaree_le', sa.DateTime(), nullable=False),
    sa.CheckConstraint('fin > debut', name='ck_indispo_fin_apres_debut'),
    sa.ForeignKeyConstraint(['medecin_id'], ['medecins.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('indisponibilites', schema=None) as batch_op:
        batch_op.create_index('ix_indispo_medecin_debut', ['medecin_id', 'debut'], unique=False)

    op.create_table('plages_consultation',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('medecin_id', sa.String(length=10), nullable=False),
    sa.Column('jour_semaine', sa.Integer(), nullable=False),
    sa.Column('debut', sa.Time(), nullable=False),
    sa.Column('fin', sa.Time(), nullable=False),
    sa.CheckConstraint('fin > debut', name='ck_plage_fin_apres_debut'),
    sa.CheckConstraint('jour_semaine BETWEEN 0 AND 6', name='ck_plage_jour'),
    sa.ForeignKeyConstraint(['medecin_id'], ['medecins.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plages_consultation', schema=None) as batch_op:
        batch_op.create_index('ix_plage_medecin_jour', ['medecin_id', 'jour_semaine'], unique=False)

    op.create_table('historique_rdv',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('rdv_id', sa.String(length=12), nullable=False),
    sa.Column('horodatage', sa.DateTime(), nullable=False),
    sa.Column('utilisateur_id', sa.String(length=12), nullable=True),
    sa.Column('action', sa.String(length=30), nullable=False),
    sa.Column('champ', sa.String(length=30), nullable=True),
    sa.Column('avant', sa.String(length=120), nullable=True),
    sa.Column('apres', sa.String(length=120), nullable=True),
    sa.ForeignKeyConstraint(['rdv_id'], ['rendez_vous.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('historique_rdv', schema=None) as batch_op:
        batch_op.create_index('ix_historique_rdv', ['rdv_id', 'horodatage'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('historique_rdv', schema=None) as batch_op:
        batch_op.drop_index('ix_historique_rdv')

    op.drop_table('historique_rdv')
    with op.batch_alter_table('plages_consultation', schema=None) as batch_op:
        batch_op.drop_index('ix_plage_medecin_jour')

    op.drop_table('plages_consultation')
    with op.batch_alter_table('indisponibilites', schema=None) as batch_op:
        batch_op.drop_index('ix_indispo_medecin_debut')

    op.drop_table('indisponibilites')
    op.drop_table('specialites')
    op.drop_table('jours_feries')

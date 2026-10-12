"""Score de risque d'absence : scores par rendez-vous et évaluations du module analytique.

Révision : 0003
Révise : 0002
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('evaluations_risque',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('calcule_le', sa.DateTime(), nullable=False),
    sa.Column('modele_retenu', sa.String(length=30), nullable=False),
    sa.Column('seuil', sa.Float(), nullable=False),
    sa.Column('auc', sa.Float(), nullable=False),
    sa.Column('nb_scores', sa.Integer(), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('evaluations_risque', schema=None) as batch_op:
        batch_op.create_index('ix_evaluations_risque_calcule_le', ['calcule_le'], unique=False)

    op.create_table('scores_risque',
    sa.Column('rdv_id', sa.String(length=12), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('a_relancer', sa.Boolean(), nullable=False),
    sa.Column('facteurs', sa.String(length=200), nullable=False),
    sa.Column('modele', sa.String(length=30), nullable=False),
    sa.Column('calcule_le', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['rdv_id'], ['rendez_vous.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('rdv_id')
    )
    with op.batch_alter_table('scores_risque', schema=None) as batch_op:
        batch_op.create_index('ix_score_a_relancer', ['a_relancer', 'score'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('scores_risque', schema=None) as batch_op:
        batch_op.drop_index('ix_score_a_relancer')
    op.drop_table('scores_risque')
    with op.batch_alter_table('evaluations_risque', schema=None) as batch_op:
        batch_op.drop_index('ix_evaluations_risque_calcule_le')
    op.drop_table('evaluations_risque')

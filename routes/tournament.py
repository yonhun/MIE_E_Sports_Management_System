from flask import Blueprint, render_template
from services import (
    tournament_history_data_loader, calculate_theoretical_rounds,
    get_tournament_or_404
)
from utils import login_required

bp = Blueprint('tournament', __name__, url_prefix='/tournament')

@bp.route("/<int:tournament_id>/history")
@login_required()
def tournament_history(tournament_id):
    """토너먼트 기록 조회 (공개)"""
    tournament = get_tournament_or_404(tournament_id)
    data = tournament_history_data_loader(tournament)
    data['total_rounds'] = calculate_theoretical_rounds(tournament_id)
    return render_template("tournament_history.html", **data)
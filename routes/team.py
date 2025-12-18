from flask import Blueprint, render_template
from models import Team
from services import get_user_score, get_member_weighted_score
from utils import login_required

bp = Blueprint('team', __name__, url_prefix='/team')

@bp.route("/<int:team_id>")
@login_required() 
def team_detail(team_id):
    """팀 상세 정보 (공용)"""
    team = Team.query.get_or_404(team_id)
    base_total = 0; weighted_total = 0
    for tm in team.members:
        u = tm.participant.user
        base = get_user_score(u)
        w_score = get_member_weighted_score(u, tm.assigned_role)
        base_total += base; weighted_total += w_score
        tm.base_score = base; tm.weighted_score = w_score
    team.base_total = base_total; team.weighted_total = weighted_total
    return render_template("team/detail.html", team=team)
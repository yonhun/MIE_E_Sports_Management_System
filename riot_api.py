import requests
from flask import current_app


def get_summoner_ranks(riot_id: str):
    """
    riot_id: "닉네임#태그" 형식 (예: "Clyde00#KR1")
    반환: (solo_tier_str, flex_tier_str, puuid)
        solo_tier_str 예: "PLATINUM IV"
        flex_tier_str 예: "GOLD III"
        랭크가 없으면 해당 값은 None
    """
    api_key = current_app.config.get("RIOT_API_KEY", "")
    if not api_key:
        return None, None, None

    if "#" not in riot_id:
        return None, None, None

    game_name, tag_line = riot_id.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()

    if not game_name or not tag_line:
        return None, None, None

    # 1단계: ASIA 계정 정보 (puuid 조회)
    account_url = (
        "https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{game_name}/{tag_line}?api_key={api_key}"
    )

    try:
        r1 = requests.get(account_url, timeout=5)
    except requests.RequestException:
        return None, None, None

    if r1.status_code != 200:
        # 필요 시 r1.text 로 디버깅
        return None, None, None

    account_data = r1.json()
    puuid = account_data.get("puuid")
    if not puuid:
        return None, None, None

    # 2단계: KR 리그 정보 (솔로/자유 랭크 리스트)
    league_url = (
        "https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/"
        f"{puuid}?api_key={api_key}"
    )

    try:
        r2 = requests.get(league_url, timeout=5)
    except requests.RequestException:
        return None, None, puuid

    if r2.status_code != 200:
        return None, None, puuid

    entries = r2.json()
    if not entries:
        # 언랭인 경우
        return None, None, puuid

    solo_tier = None
    flex_tier = None

    for e in entries:
        queue_type = e.get("queueType")
        tier = e.get("tier")
        rank = e.get("rank")

        if tier and rank:
            tier_str = f"{tier} {rank}"
        elif tier:
            tier_str = tier
        else:
            tier_str = None

        if queue_type == "RANKED_SOLO_5x5":
            solo_tier = tier_str
        elif queue_type == "RANKED_FLEX_SR":
            flex_tier = tier_str

    return solo_tier, flex_tier, puuid

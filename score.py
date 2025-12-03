# score.py

TIER_BASE = {
    "IRON": 100,
    "BRONZE": 200,
    "SILVER": 300,
    "GOLD": 400,
    "PLATINUM": 500,
    "EMERALD": 600,
    "DIAMOND": 700,
    "MASTER": 800,
    "GRANDMASTER": 900,
    "CHALLENGER": 1000,
}

DIVISION_OFFSET = {
    "IV": 0,
    "III": 25,
    "II": 50,
    "I": 75,
}


def parse_tier_rank(tier_str: str | None) -> tuple[str | None, str | None]:
    """
    "PLATINUM IV" -> ("PLATINUM", "IV")
    "MASTER" -> ("MASTER", None)
    None or "" -> (None, None)
    """
    if not tier_str:
        return None, None

    parts = tier_str.strip().split()
    if len(parts) == 1:
        return parts[0], None
    elif len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def tier_to_score(tier_str: str | None) -> int | None:
    tier, div = parse_tier_rank(tier_str)
    if not tier:
        return None

    tier = tier.upper()
    base = TIER_BASE.get(tier)
    if base is None:
        return None

    offset = 0
    if div:
        offset = DIVISION_OFFSET.get(div.upper(), 0)

    return base + offset


def calculate_estimated_score(user) -> int | None:
    """
    User 객체를 받아서 예상 점수 계산 (솔랭, 자랭 가중치 적용).
    새 정책:
      - solo_tier와 flex_tier가 모두 있으면: 솔랭(70%) + 자랭(30%)
      - solo_tier만 있으면: 솔랭(100%)
      - flex_tier만 있으면: 자랭(80%)
      - 둘 다 없으면: None
    """
    from models import User  # type: ignore

    if not isinstance(user, User):
        return None

    solo_score = tier_to_score(user.solo_tier)
    flex_score = tier_to_score(user.flex_tier)

    # 솔랭 점수 계산 (None이면 0으로 처리하여 계산에 포함되지 않도록 함)
    solo_val = solo_score if solo_score is not None else 0
    # 자랭 점수 계산 (None이면 0으로 처리하여 계산에 포함되지 않도록 함)
    flex_val = flex_score if flex_score is not None else 0

    if solo_score is not None and flex_score is not None:
        # 1. 둘 다 있는 경우: 솔랭 가중치 70%, 자랭 가중치 30%
        # 결과는 정수여야 하므로 반올림 또는 내림 처리 (여기서는 int()로 내림)
        estimated_score = int(solo_val * 0.7) + int(flex_val * 0.3)
        return estimated_score

    elif solo_score is not None:
        # 2. 솔랭만 있는 경우: 솔랭 점수 100% 반영
        return solo_score

    elif flex_score is not None:
        # 3. 자랭만 있는 경우: 자랭 점수에 80% 가중치 적용
        # 결과는 정수여야 하므로 반올림 또는 내림 처리 (여기서는 int()로 내림)
        return int(flex_val * 0.8)

    else:
        # 4. 둘 다 없는 경우
        return None

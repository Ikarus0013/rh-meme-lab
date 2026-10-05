"""The scoring model.

Five pillars, each scored 0-100 from named sub-factors, then combined by the
weights in config.WEIGHTS. Separately, hard gates mark a token DISQUALIFIED --
those are structural "you cannot trade this" conditions, deliberately kept out
of the weighted score so a great meme score can never paper over an
impossible exit.

Every sub-factor is stored with its inputs so the CLI can show *why* something
ranked where it did. A score you can't interrogate is a horoscope.
"""
import math
import re

from . import config as C

# Archetypes that recur across meme cycles. Lineage is not a guarantee of
# anything -- it is a weak prior that the token is legible to the audience that
# buys memes, which is the only thing "meme worthiness" can honestly mean.
MEME_LINEAGE = (
    "doge", "pepe", "shib", "inu", "cat", "wif", "bonk", "mog", "wojak",
    "chad", "moon", "elon", "trump", "frog", "dog", "meme", "based", "giga",
    "turbo", "brett", "toshi", "andy", "ponke", "popcat", "mew", "floki",
    "baby", "rocket", "ape", "banana", "monkey", "penguin", "pengu", "fart",
)

# Robinhood Chain returns no GT categories, but project descriptions separate
# memes from utility tokens cleanly. A revenue-share or index product can pass
# every structural filter here and still not be the thing being screened for.
MEME_MARKERS = (
    "meme", "memecoin", "community-driven", "community driven", "no utility",
    "for the culture", "degen", "culture coin", "no roadmap", "just a coin",
    "fair launch", "for fun", "shitcoin", "community token",
)
UTILITY_MARKERS = (
    "staking", "yield", "revenue", "protocol", "governance", "index fund",
    "treasury", "dividend", "apy", "buyback", "fee share", "get paid",
    "real yield", "payout", "rewards program", "utility token", "ecosystem",
)


# ------------------------------------------------------------ scoring curves
def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def ramp(x, lo, hi):
    """0 at or below `lo`, 100 at or above `hi`, linear between."""
    if hi == lo:
        return 0.0
    return clamp((x - lo) / (hi - lo) * 100.0)


def falling(x, good, bad):
    """100 at or below `good`, 0 at or above `bad`. For 'lower is better'."""
    if bad == good:
        return 0.0
    return clamp((bad - x) / (bad - good) * 100.0)


def band(x, lo, ideal_lo, ideal_hi, hi):
    """Trapezoid: 100 inside [ideal_lo, ideal_hi], tapering to 0 at lo/hi."""
    if x < lo or x > hi:
        return 0.0
    if ideal_lo <= x <= ideal_hi:
        return 100.0
    if x < ideal_lo:
        return ramp(x, lo, ideal_lo)
    return falling(x, ideal_hi, hi)


def blend(parts):
    """Weighted mean of (score, weight) pairs, skipping None scores."""
    num = den = 0.0
    for s, w in parts:
        if s is None:
            continue
        num += s * w
        den += w
    return (num / den) if den else 0.0


# --------------------------------------------------------------- derived math
def max_position_usd(liquidity, max_slippage):
    """Largest buy that stays inside a price-impact tolerance.

    Constant-product approximation: with total pool value L, the quote-side
    reserve is ~L/2, and buying X gives impact X/(Q+X). Solving for X at
    tolerance t gives X = t*Q/(1-t). Concentrated-liquidity (v3) pools can
    absorb more or less than this depending on range placement, so treat it as
    an order-of-magnitude guide, not a quote.
    """
    if liquidity <= 0 or not (0 < max_slippage < 1):
        return 0.0
    q = liquidity / 2.0
    return max_slippage * q / (1.0 - max_slippage)


def est_slippage(position, liquidity):
    if liquidity <= 0:
        return 1.0
    q = liquidity / 2.0
    return position / (q + position)


# ------------------------------------------------------------------- pillars
def score_exit(t, cfg):
    """Can I get my money back out? The pillar that matters most and is
    thought about least."""
    mcap = t.get("mcap") or t.get("mcap_gt") or 0
    liq = t.get("liquidity_ds") or t.get("liquidity") or 0
    vol24 = t.get("vol24") or 0

    liq_ratio = (liq / mcap) if mcap > 0 else 0.0
    turnover = (vol24 / liq) if liq > 0 else 0.0
    slip = est_slippage(cfg["position_usd"], liq)

    f = {}
    # Below 1% liquidity/mcap you are not really able to leave.
    # Calibrated to the chain: median token sits at ~5.4% liquidity/mcap,
    # p25 at 2.6%, p75 at 16.9%.
    f["liq_to_mcap"] = (ramp(liq_ratio, 0.012, 0.10), 0.40, f"{liq_ratio*100:.2f}%")
    # Want real two-way flow, but 50x daily turnover on a small float is churn.
    f["turnover"] = (band(turnover, 0.05, 0.8, 12.0, 60.0), 0.30, f"{turnover:.1f}x")
    # Direct answer to "can I move THIS much money in".
    f["entry_slippage"] = (falling(slip, 0.003, cfg["max_slippage"] * 2), 0.20,
                           f"{slip*100:.2f}%")
    # More venues = more ways out if one pool is drained.
    # Median token on this chain has exactly one pool, so one pool is par --
    # scored from zero rather than from one, or the whole field scores 0.
    f["route_depth"] = (ramp(t.get("pool_count", 0), 0.0, 3.0), 0.10,
                        f"{t.get('pool_count',0)}p/{t.get('dex_count',0)}d")

    return blend([(v[0], v[1]) for v in f.values()]), f


def score_holders(t, cfg):
    """Who is already in there, and is any of it real."""
    buyers = t.get("buyers24", 0)
    sellers = t.get("sellers24", 0)
    buys = t.get("buys24", 0) or t.get("ds_buys24", 0)

    ratio = (buyers / sellers) if sellers > 0 else (2.0 if buyers > 0 else 0.0)
    # Distinct buyers per buy transaction. Near 1.0 means each trade is a new
    # wallet (organic interest); near 0.2 means a handful of wallets churning,
    # which is what wash trading looks like from the outside.
    diversity = (buyers / buys) if buys > 0 else 0.0

    f = {}
    f["unique_buyers"] = (ramp(math.log10(buyers + 1), math.log10(20), math.log10(1500)),
                          0.30, f"{buyers}")
    # Accumulation is good; a 5:1 buyer/seller skew is usually bots, not demand.
    f["buy_pressure"] = (band(ratio, 0.4, 1.05, 2.5, 6.0), 0.20, f"{ratio:.2f}")
    # Chain median is 0.28 and p90 is 0.51 -- gasless account-abstraction
    # trading means wallets legitimately trade many times per day here, so the
    # organic/wash threshold sits far lower than on an L1.
    f["wallet_diversity"] = (ramp(diversity, 0.12, 0.55), 0.25, f"{diversity:.2f}")
    f["gt_holders"] = (t.get("gt_holders_score") or None, 0.25,
                       f"{t.get('gt_holders_score', 0):.0f}")

    # Deep mode: real concentration numbers replace the proxy where available.
    # Only score reconstructed holder stats when the replay recovered enough
    # of the supply to be trustworthy (see chain.replay_balances).
    hm = t.get("holder_metrics") or {}
    if hm.get("holder_count") and hm.get("reliable"):
        f["holder_count"] = (ramp(math.log10(hm["holder_count"] + 1),
                                  math.log10(50), math.log10(20000)), 0.30,
                             f"{hm['holder_count']}")
        f["top10_concentration"] = (falling(hm.get("top10_share", 1.0), 0.25, 0.80),
                                    0.40, f"{hm.get('top10_share',0)*100:.0f}%")
        # Herfindahl, not Gini. Gini over token balances is dominated by the
        # dust tail, so it tracks holder count rather than whale risk: CHUMP
        # (10.9k holders, top1 1.4%, top10 12.9%) scores gini 0.988 while the
        # far more concentrated ORBIO (3.3k holders, top1 5.9%, top10 31%)
        # scores 0.955. Scoring Gini actively punished the better-distributed
        # token. HHI squares the shares, so it responds to large holders and
        # ignores dust.
        f["hhi_concentration"] = (falling(hm.get("hhi", 1.0), 0.01, 0.15), 0.15,
                                  f"{hm.get('hhi',0):.4f}")
        f["holder_growth"] = (ramp(hm.get("new_holder_pct", 0), 0.02, 0.35), 0.15,
                              f"{hm.get('new_holder_pct',0)*100:.0f}%")

    return blend([(v[0], v[1]) for v in f.values()]), f


def score_momentum(t, cfg):
    """Is it moving, and am I early or am I the exit."""
    pct = t.get("pct") or {}
    h1 = pct.get("h1", 0.0)
    h6 = pct.get("h6", 0.0)
    h24 = pct.get("h24", 0.0)
    vol24 = t.get("vol24") or 0
    vol6 = t.get("vol6") or 0

    # 6h volume annualised to a day vs actual 24h volume: >1 means accelerating.
    accel = (vol6 * 4.0 / vol24) if vol24 > 0 else 0.0

    f = {}
    # The sweet spot is a real move that has not yet gone vertical.
    f["trend_24h"] = (band(h24, -40.0, 5.0, 80.0, 400.0), 0.25, f"{h24:+.1f}%")
    f["trend_6h"] = (band(h6, -25.0, 0.0, 60.0, 300.0), 0.20, f"{h6:+.1f}%")
    f["volume_accel"] = (ramp(accel, 0.4, 1.8), 0.25, f"{accel:.2f}x")
    # Rolling over: strongly negative on the hour after a big day = distribution.
    rollover = 100.0 if not (h1 < -8 and h24 > 40) else 15.0
    f["not_rolling_over"] = (rollover, 0.15, f"h1 {h1:+.1f}%")
    # Explicit lateness penalty, separate from the trend band.
    # Deliberately steep. Buying something already up 3x today is the single
    # most reliable way to become someone else's exit.
    f["not_parabolic"] = (falling(max(h24, 0.0), 100.0, 500.0), 0.15, f"{h24:+.0f}%")

    return blend([(v[0], v[1]) for v in f.values()]), f


def _ticker_quality(sym):
    """Crude legibility heuristic for a ticker."""
    if not sym:
        return 0.0
    s = sym.strip()
    score = 50.0
    if 3 <= len(s) <= 6:
        score += 25
    elif len(s) <= 8:
        score += 10
    else:
        score -= 20
    if re.fullmatch(r"[A-Za-z]+", s):
        score += 20
    if re.search(r"\d", s):
        score -= 15
    if re.search(r"[^A-Za-z0-9]", s):
        score -= 20
    return clamp(score)


def score_meme(t, cfg):
    """Meme worthiness: the softest pillar, so it carries the least weight and
    only measures things that are actually observable."""
    sym = (t.get("symbol") or "").lower()
    name = (t.get("token_name") or "").lower()
    blob = f"{sym} {name}"

    socials = sum([
        bool(t.get("has_twitter")), bool(t.get("has_telegram")),
        bool(t.get("has_website")), bool(t.get("has_discord")),
    ])
    branding = sum([
        bool(t.get("has_image")), bool(t.get("has_header")),
        bool(t.get("description")),
    ])
    lineage = any(k in blob for k in MEME_LINEAGE)
    age_h = t.get("age_hours") or 0.0

    desc = (t.get("description") or "").lower()
    text = f"{blob} {desc}"
    meme_hits = sum(1 for k in MEME_MARKERS if k in text)
    util_hits = sum(1 for k in UTILITY_MARKERS if k in text)
    # Neutral at 55 when the description says nothing either way.
    intent = clamp(55.0 + 18.0 * meme_hits - 22.0 * util_hits)
    t["meme_intent"] = intent
    # Below 35 the description reads as a product (yield, revenue share, index)
    # rather than a meme. Surfaced in the report; not a hard gate, because the
    # classifier is lexical and can be wrong.
    t["likely_utility"] = intent < 35.0

    f = {}
    f["social_presence"] = (ramp(socials, 0, 3), 0.28, f"{socials}/4")
    f["branding"] = (ramp(branding, 0, 3), 0.17, f"{branding}/3")
    f["gt_score"] = (t.get("gt_score") or None, 0.20, f"{t.get('gt_score',0):.0f}")
    f["ticker_quality"] = (_ticker_quality(t.get("symbol")), 0.12,
                           t.get("symbol") or "?")
    f["meme_lineage"] = (100.0 if lineage else 35.0, 0.10,
                         "yes" if lineage else "no")
    f["meme_intent"] = (intent, 0.18,
                        f"+{meme_hits}/-{util_hits}")
    # Proven enough to not be a rug rehearsal, young enough that the move is
    # not already finished.
    f["age_window"] = (band(age_h, 6, 72, 1440, 8760), 0.10, f"{age_h/24:.1f}d")

    return blend([(v[0], v[1]) for v in f.values()]), f


def score_safety(t, cfg):
    """Structural and contract risk. Penalty-shaped."""
    facts = t.get("contract") or {}
    liq = t.get("liquidity_ds") or t.get("liquidity") or 0
    sells = t.get("sells24", 0)
    buys = t.get("buys24", 0)
    sell_skew = (sells / buys) if buys > 0 else 2.0

    f = {}
    renounced = facts.get("renounced")
    f["ownership"] = (None if renounced is None else (100.0 if renounced else 45.0),
                      0.25, "renounced" if renounced else
                      ("owned" if renounced is False else "unknown"))
    f["liquidity_floor"] = (ramp(liq, 20_000, 400_000), 0.25, f"${liq:,.0f}")
    # Heavier selling than buying over a day is the market telling you something.
    f["sell_pressure"] = (falling(sell_skew, 0.9, 2.2), 0.20, f"{sell_skew:.2f}")
    f["venue_spread"] = (ramp(t.get("pool_count", 0), 0, 3), 0.15,
                         f"{t.get('pool_count',0)} pools")
    cs = facts.get("code_size")
    f["contract_present"] = (None if cs is None else (100.0 if cs > 200 else 20.0),
                             0.15, f"{cs}B" if cs is not None else "?")

    # Only score reconstructed holder stats when the replay recovered enough
    # of the supply to be trustworthy (see chain.replay_balances).
    hm = t.get("holder_metrics") or {}
    if hm.get("holder_count") and hm.get("reliable"):
        f["whale_risk"] = (falling(hm.get("top1_share", 0.5), 0.08, 0.40), 0.30,
                           f"top1 {hm.get('top1_share',0)*100:.1f}%")

    return blend([(v[0], v[1]) for v in f.values()]), f


# --------------------------------------------------------------------- gates
def check_gates(t, cfg):
    """Structural disqualifiers. Returns a list of human-readable reasons."""
    fails = []
    mcap = t.get("mcap") or t.get("mcap_gt") or 0
    liq = t.get("liquidity_ds") or t.get("liquidity") or 0
    pct = t.get("pct") or {}

    if mcap > 0 and (liq / mcap) < C.GATES["min_liq_to_mcap"]:
        fails.append(f"liquidity is {liq/mcap*100:.2f}% of mcap "
                     f"(need >{C.GATES['min_liq_to_mcap']*100:.0f}%) - cannot exit")
    if t.get("buyers24", 0) < C.GATES["min_unique_buyers"]:
        fails.append(f"only {t.get('buyers24',0)} unique buyers in 24h")
    if pct.get("h24", 0) > C.GATES["max_h24_gain"]:
        fails.append(f"already +{pct['h24']:.0f}% in 24h - you would be exit liquidity")

    hm = t.get("holder_metrics") or {}
    if hm.get("reliable") and hm.get("top10_share", 0) > C.GATES["max_top10_share"]:
        fails.append(f"top 10 wallets hold {hm['top10_share']*100:.0f}%")

    # An unknown age is not a failing age -- only gate when we actually know it.
    age_h = t.get("age_hours")
    if age_h is not None and age_h < cfg["min_age_hours"]:
        fails.append(f"only {age_h:.1f}h old")
    return fails


def score_token(t, cfg):
    """Full scoring pass. Mutates and returns the token record."""
    pillars = {}
    breakdown = {}
    for name, fn in (("exit", score_exit), ("holders", score_holders),
                     ("momentum", score_momentum), ("meme", score_meme),
                     ("safety", score_safety)):
        s, f = fn(t, cfg)
        pillars[name] = s
        breakdown[name] = f

    t["pillars"] = pillars
    t["breakdown"] = breakdown
    t["score"] = sum(pillars[k] * C.WEIGHTS[k] for k in C.WEIGHTS)
    t["gate_failures"] = check_gates(t, cfg)
    t["disqualified"] = bool(t["gate_failures"])
    liq = t.get("liquidity_ds") or t.get("liquidity") or 0
    t["max_position"] = max_position_usd(liq, cfg["max_slippage"])
    return t

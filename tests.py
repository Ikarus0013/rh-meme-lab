"""Offline tests for the pure logic: scoring curves, slippage math, and the
Transfer-log replay. No network required.

    python3 tests.py
"""
from memescan import chain, config as C, score as S

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        fails.append(name)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


print("scoring curves")
check("ramp clamps low", S.ramp(0, 10, 20) == 0)
check("ramp clamps high", S.ramp(99, 10, 20) == 100)
check("ramp midpoint", approx(S.ramp(15, 10, 20), 50))
check("falling inverts", approx(S.falling(15, 10, 20), 50))
check("falling clamps", S.falling(5, 10, 20) == 100 and S.falling(25, 10, 20) == 0)
check("band plateau", S.band(50, 0, 40, 60, 100) == 100)
check("band outside", S.band(-1, 0, 40, 60, 100) == 0 and S.band(101, 0, 40, 60, 100) == 0)
check("band ramps up", approx(S.band(20, 0, 40, 60, 100), 50))
check("band ramps down", approx(S.band(80, 0, 40, 60, 100), 50))
check("degenerate ramp", S.ramp(5, 10, 10) == 0)

print("\nblend")
check("skips None", approx(S.blend([(100, 1), (None, 5), (0, 1)]), 50))
check("all None -> 0", S.blend([(None, 1)]) == 0.0)
check("weights honoured", approx(S.blend([(100, 3), (0, 1)]), 75))

print("\nslippage math")
# Constant product: quote reserve Q = L/2, impact = X/(Q+X).
# L=100k -> Q=50k; at 2% tolerance X = .02*50000/.98 = 1020.408...
check("max position at 2%", approx(S.max_position_usd(100_000, 0.02), 1020.4081632653, 1e-6))
check("round trip", approx(S.est_slippage(S.max_position_usd(100_000, 0.02), 100_000), 0.02, 1e-9))
check("zero liquidity -> no size", S.max_position_usd(0, 0.02) == 0.0)
check("zero liquidity -> total slip", S.est_slippage(1000, 0) == 1.0)
check("bigger pool absorbs more",
      S.max_position_usd(1_000_000, 0.02) > S.max_position_usd(100_000, 0.02))

print("\nticker quality")
check("clean ticker beats messy", S._ticker_quality("PEPE") > S._ticker_quality("P3P3$!!"))
check("empty ticker", S._ticker_quality("") == 0.0)

print("\ntransfer replay")
ZERO = "0x" + "0" * 64
def topic(addr):  return "0x" + "0" * 24 + addr[2:]
def log(frm, to, val, blk=1):
    return {"topics": [C.TRANSFER_TOPIC, topic(frm), topic(to)],
            "data": hex(val), "blockNumber": hex(blk)}

A = "0x" + "a" * 40
B = "0x" + "b" * 40
POOL = "0x" + "c" * 40
Z = "0x" + "0" * 40

# mint 1000 to A; A sends 400 to B; A sends 100 to the pool
logs = [log(Z, A, 1000, 1), log(A, B, 400, 2), log(A, POOL, 100, 3)]
rp = chain.replay_balances(logs, exclude=[POOL], total_supply=1000)
check("coverage is exact", approx(rp["coverage"], 1.0), f"got {rp['coverage']}")
check("pool excluded from holders", POOL.lower() not in rp["holders"])
check("holder balances correct",
      rp["holders"][A.lower()] == 500 and rp["holders"][B.lower()] == 400)
check("zero address dropped", Z.lower() not in rp["holders"])
check("excluded supply counted", rp["excluded_supply"] == 100)
check("transfer count", rp["n_transfers"] == 3)

# A truncated scan: B's earlier purchase is missing, so B goes net-negative
truncated = [log(B, A, 400, 9)]
rp2 = chain.replay_balances(truncated, exclude=[], total_supply=1000)
check("truncated scan shows low coverage", rp2["coverage"] < 0.9,
      f"got {rp2['coverage']}")
hm2 = chain.holder_metrics(rp2, head=100, window_blocks=10)
check("truncated scan marked unreliable", hm2["reliable"] is False)

# Gaps must poison reliability even at full coverage.
rp3 = chain.replay_balances(logs, exclude=[POOL], total_supply=1000,
                            skipped=[(5, 10)])
hm3 = chain.holder_metrics(rp3, head=100, window_blocks=10)
check("gaps force unreliable", hm3["reliable"] is False and hm3["gaps"] == 1)

# ERC-721 Transfers (4 topics) must be ignored.
nft = [{"topics": [C.TRANSFER_TOPIC, topic(A), topic(B), "0x01"],
        "data": "0x", "blockNumber": "0x1"}]
check("ERC-721 ignored", chain.replay_balances(nft)["n_transfers"] == 0)

print("\nconcentration")
# Start well above 0x0..1, which is a burn address and correctly excluded.
def synth(i):  return "0x" + f"{i + 0x1000:040x}"

many = [log(Z, synth(i), 100, 1) for i in range(100)]
rpe = chain.replay_balances(many, total_supply=10_000)
hme = chain.holder_metrics(rpe, head=10, window_blocks=100)
check("100 equal holders", hme["holder_count"] == 100)
check("even distribution -> low gini", hme["gini"] < 0.05, f"gini={hme['gini']:.4f}")
check("top10 share is 10%", approx(hme["top10_share"], 0.10, 1e-9))
check("hhi = 1/n", approx(hme["hhi"], 0.01, 1e-9))

skew = [log(Z, A, 9000, 1)] + [log(Z, synth(i), 100, 1) for i in range(10)]
hms = chain.holder_metrics(chain.replay_balances(skew, total_supply=10_000),
                           head=10, window_blocks=100)
check("skewed -> high gini", hms["gini"] > 0.7, f"gini={hms['gini']:.4f}")
check("skewed top1 = 90%", approx(hms["top1_share"], 0.9, 1e-9))

# Burn addresses must never count as holders, or every token looks concentrated.
burn = [log(Z, A, 500, 1),
        log(Z, "0x000000000000000000000000000000000000dead", 500, 1)]
rpb = chain.replay_balances(burn, total_supply=1000)
check("burn address excluded from holders", rpb["holders"] == {A.lower(): 500})
check("burn still counts toward coverage", approx(rpb["coverage"], 1.0))

# A v4 singleton PoolManager shows up as an ordinary holder; excluding it must
# change the concentration picture rather than leave it whale-dominated.
POOLMGR = "0x" + "e" * 40
v4 = [log(Z, POOLMGR, 6000, 1)] + [log(Z, synth(i), 1000, 1) for i in range(4)]
rp4 = chain.replay_balances(v4, exclude=[], total_supply=10_000)
hm_in = chain.holder_metrics(rp4, head=10, window_blocks=100)
hm_out = chain.holder_metrics(rp4, head=10, window_blocks=100,
                              contract_holders=[POOLMGR])
check("singleton dominates if not excluded", approx(hm_in["top1_share"], 0.6, 1e-9))
check("excluding singleton fixes top1", approx(hm_out["top1_share"], 0.25, 1e-9))
check("contract supply reported", approx(hm_out["contract_supply_pct"], 0.6, 1e-9))
check("contract holder counted", hm_out["contract_holders"] == 1)
check("wallet count drops by one",
      hm_out["holder_count"] == hm_in["holder_count"] - 1)

# 32-byte v4 pool IDs in the exclude list must not break anything.
rp5 = chain.replay_balances(v4, exclude=["0x" + "f" * 64], total_supply=10_000)
check("32-byte pool id ignored safely", rp5["holders"] == rp4["holders"])

# Regression, from two real tokens with verified 100%-coverage holder data:
# CHUMP (10,913 holders, top1 1.4%, top10 12.9%) is better distributed than
# ORBIO (3,301 holders, top1 5.9%, top10 31.0%), but has the HIGHER Gini
# (0.988 vs 0.955) because Gini over token balances tracks the dust tail.
# Any concentration factor must rank CHUMP ahead of ORBIO.
print("\nconcentration metric choice")
chump = {"hhi": 0.0035, "gini": 0.988, "top10_share": 0.129}
orbio = {"hhi": 0.0153, "gini": 0.9546, "top10_share": 0.310}
hhi_c = S.falling(chump["hhi"], 0.01, 0.15)
hhi_o = S.falling(orbio["hhi"], 0.01, 0.15)
top_c = S.falling(chump["top10_share"], 0.25, 0.80)
top_o = S.falling(orbio["top10_share"], 0.25, 0.80)
check("HHI ranks better-distributed token higher", hhi_c > hhi_o)
check("top10 ranks it higher too", top_c > top_o)
check("HHI agrees with top10 ordering", (hhi_c > hhi_o) == (top_c > top_o))
# The trap this replaced: Gini ordered them backwards.
g_c = S.falling(chump["gini"], 0.75, 0.98)
g_o = S.falling(orbio["gini"], 0.75, 0.98)
check("Gini would have ranked them backwards (why it is unscored)", g_c < g_o)

print("\nEIP-7702 delegated EOAs")
d = "0xef0100" + "a" * 40                       # 23 bytes
check("delegation recognised", chain.is_delegated_eoa(d))
check("real contract not delegation", chain.is_delegated_eoa("0x60808060" + "0" * 200) is False)
check("empty code not delegation", chain.is_delegated_eoa("0x") is False)
check("wrong length not delegation", chain.is_delegated_eoa("0xef0100" + "a" * 60) is False)
check("non-string safe", chain.is_delegated_eoa(None) is False)

print("\ngates")
cfg = dict(C.DEFAULTS)
thin = {"mcap": 50_000_000, "liquidity": 100_000, "buyers24": 500,
        "pct": {"h24": 10}, "age_hours": 100}
check("thin liquidity gated", any("cannot exit" in g for g in S.check_gates(thin, cfg)))
ok = {"mcap": 50_000_000, "liquidity": 3_000_000, "buyers24": 500,
      "pct": {"h24": 10}, "age_hours": 100}
check("healthy token passes", S.check_gates(ok, cfg) == [])
unknown_age = dict(ok); unknown_age["age_hours"] = None
check("unknown age does not gate", S.check_gates(unknown_age, cfg) == [])
pumped = dict(ok); pumped["pct"] = {"h24": 1500}
check("parabolic gated", any("exit liquidity" in g for g in S.check_gates(pumped, cfg)))

print("\nweights")
check("pillar weights sum to 1", approx(sum(C.WEIGHTS.values()), 1.0))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
raise SystemExit(1 if fails else 0)

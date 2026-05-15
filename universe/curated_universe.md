# Curated Universe
#
# Pre-filtered large-cap tickers for the wheel screener — see
# `docs/stock_universe.md` for inclusion rules (market cap >= $10B,
# options-liquid, exclude financials with tier-1 exception, ETFs/indexes
# allowed). These run through the full screening criteria every scan,
# alongside anything listed in `state/manual_universe.md`.
#
# Format: one ticker per line. Blank lines and lines starting with `#`
# are ignored. Section headers (`# --- Sector ---`) are visual only and
# do not affect parsing. Maintain the list manually; review periodically.

# --- Information Technology ---
AAPL
MSFT
NVDA
AVGO
ORCL
AMD
ADBE
CRM
INTU
NOW
ACN
CSCO
AMAT
MU
KLAC
LRCX
ASML
TXN
QCOM
INTC
ANET
PANW
CRWD
FTNT
SNPS
CDNS
IBM
MSTR

# --- Communication Services ---
GOOGL
GOOG
META
NFLX
DIS
TMUS
CMCSA
T
VZ
EA

# --- Consumer Discretionary ---
AMZN
TSLA
HD
MCD
LOW
NKE
SBUX
BKNG
MAR
CMG
TGT
ABNB

# --- Consumer Staples ---
WMT
COST
PG
KO
PEP
MDLZ
PM
MO
CL
KMB

# --- Health Care ---
LLY
JNJ
UNH
ABBV
MRK
TMO
PFE
ABT
DHR
AMGN
ISRG
BMY
MDT
ELV
GILD
REGN
VRTX
CI

# --- Industrials ---
CAT
GE
BA
HON
RTX
LMT
UNP
UPS
DE
ETN
EMR
ITW
PH
NOC
GD
UBER

# --- Materials ---
LIN
SHW
FCX
ECL
APD
NEM
DD

# --- Energy ---
XOM
CVX
COP
EOG
OXY
MPC
PSX
SLB

# --- Utilities ---
NEE
DUK
SO
AEP
D

# --- Real Estate ---
AMT
PLD
EQIX
CCI
SPG

# --- Financials (Tier-1 non-credit only, per docs/stock_universe.md) ---
V
MA
SPGI
MCO
MSCI
ICE
CME
BRK.B
COIN

# --- Financials (Banks) — bank-specific criteria, see docs/stock_universe.md §5b ---
GS
MS
JPM

# --- ETFs / Indexes ---
SPY
QQQ
IWM
DIA
EFA

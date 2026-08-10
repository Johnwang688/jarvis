# AI Infrastructure Paper Portfolio Research

**Prepared 2 August 2026 (CDT)** | Hypothetical $100,000 | 12–36 months | Paper research only

> No leverage, margin, options, or actual trading. This is not individualized financial advice. Facts are sourced; portfolio weights, returns, correlations, and stress outcomes are illustrative assumptions, not forecasts.

## Executive conclusion

The causal chain is: **AI adoption/workloads -> training and inference -> accelerators and HBM -> servers/racks -> networking/storage -> data-center construction, power and cooling -> generation, transmission, transformers and grid interconnection.** The thesis is attractive because compute and data-center electricity demand may rise for years, but it is one interconnected trade. The key question is whether AI revenue and productivity monetize faster than hyperscaler capital intensity, supply additions, power delays, and valuation compression.

The IEA reported that data centers used about **415 TWh globally in 2024**, around 1.5% of electricity consumption, and its base case projects more than 2x demand to about **945 TWh by 2030**; AI-optimized data-center electricity use is projected to quadruple. These are scenarios, not guarantees. Source: IEA, *Energy and AI*, 10 April 2025: https://www.iea.org/reports/energy-and-ai

A 24-month candidate keeps 15% in Treasury bills, limits a single company to 13%, and spreads exposure across semiconductors, networking/power, data centers, cloud/software, and reserve. A severe illustrative shock still loses about 41%; the reserve reduces forced selling but does not make the portfolio safe.

## 1. Investable universe, dependencies, and bottlenecks

| Layer | Examples | Dependency / bottleneck |
|---|---|---|
| Accelerators | NVDA, AMD, SMH, SOXX | Workload growth, product cycles, software ecosystems, export controls |
| Memory/foundry/equipment | MU, TSM, ASML | HBM, advanced packaging, leading-edge capacity, geopolitics |
| Networking | AVGO, ANET | Cluster scale, Ethernet/optics, hyperscaler capex |
| Power/cooling/electrical | VRT, ETN, GEV | High-density racks, liquid cooling, transformers, permits |
| Generation/grid | CEG, GEV, ETN | Firm power, interconnection, transmission, regulation |
| Data centers | EQIX, DLR, SRVR | Powered land, construction, water, connectivity, rates |
| Cloud/platforms | MSFT, AMZN, GOOGL, IGV | AI monetization, capex returns, competition, regulation |

Demand must be paid for; accelerators need HBM, packaging, substrates, optics and foundries; clusters need high-bandwidth networking; power/cooling/construction and permits can delay deployments; hyperscalers must earn acceptable returns on enormous capex. A bottleneck can be bullish for its supplier but bearish for the chain if it delays revenue.

## 2. Shortlist: exposure, catalysts, risks, falsifiers

Valuation ratios for 2 August 2026 were not reliably retrievable here, so none below is presented as current fact. Before tracking, record dated price, diluted shares, market cap, EV, forward/trailing P/E, EV/sales, EV/EBITDA, price/FCF, FCF yield and net debt/EBITDA. For EQIX/DLR use P/FFO; for MU/TSM/ASML use normalized mid-cycle measures.

| Ticker | Exposure; primary source | Catalyst | Risks / falsifier |
|---|---|---|---|
| NVDA | GPUs, accelerated computing, networking, CUDA/software; https://investor.nvidia.com/financial-info/annual-reports-and-proxies/default.aspx | New accelerator cycles, inference, networking | Custom ASICs, competition, controls, concentration, valuation; falsifier: sustained data-center deceleration/share loss |
| AMD | EPYC CPUs, Instinct accelerators; https://ir.amd.com/financial-information/annual-reports-and-proxies | Instinct adoption, server share | Ecosystem/execution, controls, PC cycle; falsifier: weak accelerator traction |
| AVGO | Custom silicon, switching, connectivity/optics, infrastructure software; https://investors.broadcom.com/financial-information/annual-reports-and-proxies | Custom AI ASICs, Ethernet, VMware synergies | Concentration, integration/debt, cycles; falsifier: networking growth stalls |
| TSM | Leading-edge foundry and packaging; https://investor.tsmc.com/english/annual-reports | HPC/AI utilization, nodes, packaging | Taiwan, hazards, concentration, capex; falsifier: persistent utilization/margin decline |
| MU | DRAM/NAND and HBM; https://investors.micron.com/financial-information/annual-reports | HBM pricing/volume | Memory oversupply/cycle; falsifier: HBM demand disappoints |
| ASML | Critical lithography equipment; https://www.asml.com/en/investors/financial-results/annual-reports | Leading-edge fab investment | Orders, controls, customer capex; falsifier: sustained cancellations |
| ANET | Cloud/AI switches and network software; https://investors.arista.com/financial-information/annual-reports-and-proxies | Cluster expansion, Ethernet | Hyperscaler concentration/capex digestion; falsifier: order reversal |
| VRT | Data-center power, cooling, monitoring; https://investors.vertiv.com/financial-information/annual-reports-and-proxies | Liquid cooling, high-density buildout | Valuation, execution, timing; falsifier: falling orders/backlog/margins |
| ETN | Electrical distribution/quality and electrification; https://www.eaton.com/us/en-us/company/investor-relations/annual-reports.html | Data-center and grid orders | Industrial cycle, delays, materials; falsifier: no backlog growth |
| GEV | Generation, wind, electrification/grid equipment; https://investor.gevernova.com/financial-information/annual-reports | Generation/grid modernization | Policy, wind economics, execution; falsifier: unprofitable backlog |
| CEG | U.S. nuclear generation; https://investors.constellationenergy.com/financial-information/annual-reports | Firm-power contracts/data-center load | Outages, regulation, prices, valuation; falsifier: weak contracted economics |
| EQIX/DLR | Data-center ownership, development, interconnection; https://investor.equinix.com/financial-information/annual-reports-and-proxies and https://investor.digitalrealty.com/financial-information/annual-reports | Powered capacity, leasing, connectivity | Rates, leverage, power/permitting; falsifier: leasing/returns below funding cost |
| MSFT/AMZN/GOOGL | Azure/AWS/Google Cloud, AI services, chips and data centers; https://www.microsoft.com/en-us/Investor/annual-reports.aspx ; https://www.aboutamazon.com/investor-relations ; https://abc.xyz/investor/ | Cloud AI monetization, custom silicon | Capex outruns returns, competition, antitrust; falsifier: AI revenue fails to cover incremental infrastructure |
| SMH/SOXX/SRVR | Semiconductor and digital-infrastructure ETFs; https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/ ; https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf ; https://www.paceretfs.com/products/srvr | Diversification within sleeve | Look-through overlap, beta, cycles, REIT rates |

## 3. Concentration and hidden factor risks

NVDA, AVGO, ANET, VRT, EQIX, DLR and electrical suppliers depend directly or indirectly on hyperscaler capex. SMH/SOXX may duplicate direct chip holdings. Growth stocks and REITs are sensitive to real rates; chips/memory are cyclical; TSM adds Taiwan/ADR risk; CEG/GEV add policy and power-price risk. Export controls, tariffs, permitting, water, transmission and transformer scarcity can change the addressable market.

Correlations rise in selloffs. A recent-boom covariance matrix understates stress correlation. Use 2–5 years of weekly returns plus factors for broad equity, growth/real yields, semiconductor cycle, hyperscaler capex, REIT/utilities, USD/ADR and geopolitics. No leverage avoids margin calls and forced liquidation; this account uses no borrowing or derivatives.

## 4. Quantitative paper-account method

Collect quarterly revenue, AI-relevant segment proxy, growth, margins, FCF, capex, backlog/orders, diluted shares, net debt, interest coverage, customer/geographic concentration, valuation, beta, 60/252-day volatility, maximum drawdown, liquidity, ETF holdings and factor exposures.

Quarterly score (0–100): `0.30 earnings momentum + 0.20 FCF/reinvestment + 0.20 valuation + 0.15 balance sheet + 0.10 AI relevance + 0.05 risk quality.` Scores 80–100 get full target, 70–79 get 75%, 60–69 get 50%, below 60 is excluded except through an ETF. Look-through caps: semiconductors/equipment 50%; networking/power/cooling/electrification 25%; data centers 10%; cloud/software 15%; one company 15%; reserve 8–25% by horizon.

For optimization maximize `w'μ − λw'Σw`, subject to caps, weights summing to one and reserve floor. μ is a conservative scenario return adjusted for score/valuation; Σ is shrinkage covariance plus the factors above. If unstable, use inverse-volatility or equal-risk weights within sleeves. Rebalance quarterly, or when a position is 25% from target, a cap is breached, or a thesis event occurs. Exit/reduce only for evidence: two quarters of relevant-revenue/backlog deterioration plus margin decline, balance-sheet impairment, lost product position, or permanent policy/geopolitical change—not an arbitrary price stop.

## 5. Candidate constructions

Fractional shares, taxes, spreads and ETF tracking error are ignored. T-bill/SGOV reserve is justified by horizon: a 12-month account has more timing risk and less time for earnings to compound; reserve also prevents forced selling. It is not a market-timing fund.

### 12 months — 25% reserve

| Holding | % | $ |
|---|---:|---:|
| T-bills/SGOV | 25 | 25,000 |
| NVDA | 12 | 12,000 |
| AVGO | 10 | 10,000 |
| AMD | 6 | 6,000 |
| TSM | 7 | 7,000 |
| ASML | 5 | 5,000 |
| SMH | 8 | 8,000 |
| ANET | 7 | 7,000 |
| VRT | 6 | 6,000 |
| ETN | 4 | 4,000 |
| EQIX | 5 | 5,000 |
| IGV | 5 | 5,000 |

### 24 months — 15% reserve (baseline)

| Holding | % | $ |
|---|---:|---:|
| T-bills/SGOV | 15 | 15,000 |
| NVDA | 13 | 13,000 |
| AVGO | 10 | 10,000 |
| AMD | 6 | 6,000 |
| TSM | 7 | 7,000 |
| ASML | 5 | 5,000 |
| SMH | 7 | 7,000 |
| ANET | 8 | 8,000 |
| VRT | 7 | 7,000 |
| ETN | 5 | 5,000 |
| EQIX | 5 | 5,000 |
| MSFT | 5 | 5,000 |
| IGV | 7 | 7,000 |

### 36 months — 8% reserve

| Holding | % | $ |
|---|---:|---:|
| T-bills/SGOV | 8 | 8,000 |
| NVDA | 13 | 13,000 |
| AVGO | 11 | 11,000 |
| AMD | 6 | 6,000 |
| TSM | 7 | 7,000 |
| ASML | 5 | 5,000 |
| SMH | 7 | 7,000 |
| ANET | 10 | 10,000 |
| VRT | 8 | 8,000 |
| ETN | 6 | 6,000 |
| EQIX | 7 | 7,000 |
| MSFT | 10 | 10,000 |
| IGV | 2 | 2,000 |

Approximate sleeve weights: semiconductors 48%, 48%, 49%; networking/power 17%, 20%, 24%; data centers 5%, 5%, 7%; software 5%, 12%, 12%; reserve 25%, 15%, 8% (12/24/36 months).

## 6. Scenarios and stress tests

Illustrative annual sleeve assumptions: base semiconductors +25%, networking/power +20%, data centers +15%, software +18%, bills +4.5%; bear −45%, −35%, −25%, −30%, +4.5%; bull +55%, +40%, +30%, +30%, +4.5%.

| Portfolio | Bear annual | Base annual | Bull annual |
|---|---:|---:|---:|
| 12-month | −29.2% | +18.2% | +37.3% |
| 24-month | −32.8% | +19.6% | +40.2% |
| 36-month | −35.4% | +20.6% | +42.6% |

Compounded base cases are approximately +18.2%, +43.0%, +75.5% over 12/24/36 months, but are highly uncertain. A harsher instantaneous shock (semiconductors −55%, networking/power −45%, data centers −35%, software −35%, bills +4.5%) gives approximate losses of −36.4%, −40.7%, and −44.0%.

Named tests: (1) AI-capex slowdown—cut semiconductor/networking/power assumptions and test inventory, backlog and margin effects; (2) rates rising—apply a 100–200 bp real-rate shock and compress growth/REIT multiples; (3) semiconductor downturn—model memory/foundry/equipment down 40–60% and correlations near 1; (4) power-bottleneck delay—delay data-center capacity conversion 4–8 quarters; (5) broad equity selloff—apply beta shock plus correlation spike. Benchmarks: SPY, QQQ, SOXX/SMH, and 3-month Treasury-bill total return.

## 7. Limitations and exact pre-start verification

Limitations: no verified live 2 August 2026 prices/ratios here; fiscal periods differ; AI revenue is inconsistently disclosed; ETF holdings change; scenarios are subjective; covariance is nonstationary; geopolitical/policy outcomes are unknowable; taxes, spreads, currency and tracking error are omitted; a 12–36 month horizon may not recover a drawdown.

Before starting, verify: (1) dated closing prices, splits, shares, market cap and EV; (2) latest 10-Q/10-K and earnings release for revenue, margin, FCF, capex, backlog and customer concentration; (3) forward estimates and valuation denominators; (4) ETF holdings, weights, expense ratios and overlap; (5) Treasury-bill yield and maturity ladder; (6) current export-control, tariff, tax-credit, nuclear, permitting and interconnection rules; (7) recent correlations, volatility, drawdowns and liquidity; (8) exact paper-account start/end dates and benchmark methodology.

## References

- IEA, *Energy and AI* (10 Apr. 2025): https://www.iea.org/reports/energy-and-ai
- NVIDIA investor relations: https://investor.nvidia.com/
- AMD investor relations: https://ir.amd.com/
- Broadcom investor relations: https://investors.broadcom.com/
- TSMC investor relations: https://investor.tsmc.com/
- Micron investor relations: https://investors.micron.com/
- ASML investor relations: https://www.asml.com/en/investors
- Arista investor relations: https://investors.arista.com/
- Vertiv investor relations: https://investors.vertiv.com/
- Eaton investor relations: https://www.eaton.com/us/en-us/company/investor-relations.html
- GE Vernova investor relations: https://investor.gevernova.com/
- Constellation investor relations: https://investors.constellationenergy.com/
- Equinix and Digital Realty investor relations: https://investor.equinix.com/ and https://investor.digitalrealty.com/
- Microsoft, Amazon and Alphabet investor relations: https://www.microsoft.com/en-us/Investor/ ; https://www.aboutamazon.com/investor-relations ; https://abc.xyz/investor/
- ETF pages: https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/ ; https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf ; https://www.paceretfs.com/products/srvr

# InvenTree Production Cost Optimizer

Version **0.1.0** — **read-only pilot for replica testing**.

An InvenTree Part-tab plugin that compares incremental purchasing expenditure for a production run across build quantities, traversing nested subassembly BOMs and consuming existing usable component/subassembly stock.

## Install on the replica

1. In Admin Center → Plugins, install from `git+https://github.com/bmalatest-dev/inventree-production-cost-optimizer-plugin.git`.
2. Restart the InvenTree server after installation, as requested by the plugin manager.
3. Activate **Production Cost Optimizer** if necessary.
4. Open an assembled Part (e.g., VR15), find its **Production Cost Optimizer** tab, select a build quantity range and click **Calculate**.

No database migrations or plugin-specific tables are used. The analysis does not create builds, purchases or allocations.

## v0.1.0 calculation rules

- The selected quantity means *new finished products to manufacture*, irrespective of existing finished stock.
- Traverse nested BOMs; consume eligible in-house subassemblies before opening their BOMs.
- Consolidate demand for shared parts before subtracting any inventory. Stock is never reused between branches.
- Restrict stock offsets to unallocated stock in normal stock status (10), without a custom status and not installed/consumed.
- Use BOM quantity, attrition, setup quantity and rounding multiple where provided. Skip optional BOM lines.
- Historical completed purchase-order unit prices in CAD take priority; lowest recorded CAD supplier price-break unit price is fallback.
- Every unpriced part is explicitly flagged. **Known spend is only a lower bound until all required items are priced.**
- Scenario comparisons include known purchasing spend, known spend per unit and successive-quantity marginal increments, plus a shortage list and CSV export.

## Important limitations in this pilot

- Purchase-order completion status, supplier price-break relation, available-stock property and BOM fields should be confirmed against your 1.8.0-dev database using the replica. The pricing adapter is deliberately conservative, and may report missing prices until verified.
- Approved substitutes, per-location priorities, PO arrivals, purchase minimum order quantities / order multiples, supplier currency conversion and assembly-service costs are **not yet supported**. Do not use the current estimates to authorize purchasing.
- No FX assumptions are made. Prices not denominated in CAD are treated as missing rather than silently converted.
- Unit economics exclude labor, duties, freight and taxes, and do not represent accounting COGS.
- A complete cost estimate may still omit optional lines or costs not present in the InvenTree BOM. Review the purchase breakdown with manufacturing before using it.
- Build ranges are capped at 200 finished units and the BOM traversal at 10,000 parts for the pilot.

## Local unit tests

```bash
python -m unittest discover -s tests -v
```

Start with a VR15 quantity range spanning a known FPGA stock threshold. Compare stock, BOM quantities and known supplier costs against InvenTree before trusting any recommendation.

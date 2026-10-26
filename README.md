# backpacking-meals-db

Nutrition facts, ingredients, and package weights for commercial backpacking meals, in SQLite.

| Brand | Products | Nutrition panel on the site | How it got in the DB |
|---|---|---|---|
| Mountain House | 78 | HTML table | parsed with BeautifulSoup |
| Backpacker's Pantry | 33 | image | read from the label image by a generative AI model |
| Peak Refuel | 26 | image | read from the label image by a generative AI model |
| Huel (Hot & Savory, powders) | 29 | image | read from the label image by a generative AI model |

166 purchasable portions total (see `v_optimizer`). Snapshot taken 2026-10-09; products and formulas change, so re-run before relying on it.

## Why the labels were read by a vision model

Only Mountain House publishes the panel as text. The other three ship a PNG/JPG of the label. The OCR stage (`ocr`, tesseract) is in the script, but I don't use its output: on dual-column panels (per serving | per container) it dropped values and misread digits in ways the sanity check below could not catch (saturated fat 6 g read for 39 g; total fat 20 g for 10 g, both still within tolerance of the label calories).

Instead, the label images were read and coded by generative AI image reading (Claude), using the **per-container** column, and written to `manual_nutrition.csv`. A sample was hand checked for efficacy; `make_verify_page.py` builds a side-by-side page (label image next to the stored values) for that check. Page text (calories, protein, net weight, servings, ingredients) is parsed automatically and used as a cross-check. AI reading can still make mistakes that the Atwater check does not catch, so treat image-read rows as verified-by-sample, not verified-by-row.

## Pipeline

```
python scrape_foods.py all              # discover, fetch, ocr, manual, export, report
python scrape_foods.py discover fetch --brand mountain_house
python scrape_foods.py crops --brand peak    # contact sheets of label crops in cache/sheets/
python scrape_foods.py manual export         # apply manual_nutrition.csv, rebuild foods.csv/json
```

- `discover`: Shopify `products.json` for Mountain House, Backpacker's Pantry and Peak Refuel; the sitemap for Huel. Non-food (gear, bundles, kits) is classified out.
- `fetch`: parses each product page. Raw HTML is cached in `cache/` (git-ignored) and requests are throttled to 1 per second per host.
- `ocr`: optional, see above. `crops` writes cropped label images so they can be read and coded into `manual_nutrition.csv`.
- `manual`: applies `manual_nutrition.csv` (keyed by product handle). Rows with `kind=skip` drop a product (non-label images, duplicates).

Requires Python 3.9+, `requests`, `beautifulsoup4`, `lxml`; Pillow and `tesseract` only for `ocr`/`crops`.

## Schema

`products` (one row per product or flavor), `nutrition` (one row per product, per `basis`), `ingredients`. Convenience views:

- `v_meals`: everything joined.
- `v_optimizer`: one row per **portion a hiker carries and eats**.

### Basis and portion scaling

Labels state values per serving or per package. `nutrition.basis` records which. A `portion` is:

- the whole package when `basis = 'package'` or `servings_per_container <= 2` (pouches, single-serve meals), or
- one serving otherwise (#10 cans, Huel powder tubs).

With `s` = servings per container, a per-serving value `x` becomes `x * s` for a whole package; a per-package value becomes `x / s` for a serving. Prices scale the same way.

### Weight provenance (`weight_src`)

| `weight_src` | Meaning | Rows |
|---|---|---|
| `page` | net weight printed on the product page | 59 |
| `label` | net weight / serving grams read from the label | 29 |
| `derived` | servings x grams per serving in the serving size, e.g. 2 x 46 g (Mountain House does not list net weight; this is **dry mix**) | 62 |
| `shipping` | Shopify variant shipping weight, which **includes packaging** and is an overestimate | 16 |

The 16 `shipping` rows are Mountain House Pro-Paks. Treat them as upper bounds.

### Sanity checks

For each row with fat `F`, carbohydrate `C` and protein `P` in grams, the Atwater estimate `4P + 4C + 9F` should be within 20% of the labeled calories; saturated fat must not exceed total fat, and sugars must not exceed carbohydrate. Rows that fail are flagged `needs_review`. As the OCR experience shows, this catches gross errors only.

### Known source conflicts

Page text and label image disagree for two Peak Refuel products. For both, the page's calories and protein are used and the row is flagged `needs_review` with a note (other nutrients come from the label and may be off):

| Product | Page | Label image |
|---|---|---|
| MeatEater American Buffalo Goulash | 740 kcal, 45 g protein | 890 kcal, 55 g protein |
| Butternut Dal Bhat | 920 kcal, 24 g protein | 870 kcal, 23 g protein |

All other Peak and Backpacker's Pantry products agree between page and label within 3% (calories) and 8% (protein). A physical pouch would settle which is current.

## Limits

- Facts only; no images are redistributed. Brand, product, and ingredient text belong to their owners.
- Mountain House Pro-Pak weights are shipping weights (above). Mountain House pouch weights are dry-mix estimates.
- Huel powders are bulk products; their `v_optimizer` rows are single servings.
- Labels can differ from what is on shelves now.

## Using it in the food optimizer

`foods.json` / `foods.csv` are the export of `v_optimizer` plus ingredients and allergens. Each row has `portion_g`, `kcal`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `sodium_mg`, `kcal_per_oz`, `price_usd`, and `weight_src`.

## License

MIT for the code. Data are published product facts, gathered 2026-10-09.

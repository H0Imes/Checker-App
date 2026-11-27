import { mkdir, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const output = process.argv[2] ?? 'release/billa-prices.json';
const endpoint = 'https://shop.billa.at/api/product-discovery/products';
const pageSize = 500;
const checkedAt = new Date().toISOString();

async function readPage(page) {
  const endpointUrl = new URL(endpoint);
  endpointUrl.search = new URLSearchParams({page, pageSize});
  const response = await fetch(endpointUrl, {signal: AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error(`BILLA page ${page} returned HTTP ${response.status}`);
  const data = await response.json();
  if (!Number.isInteger(data.total) || data.total <= 0 ||
      data.isTotalTruncated !== false || data.offset !== page * pageSize ||
      !Array.isArray(data.results) ||
      data.results.length !== Math.min(pageSize, data.total - data.offset)) {
    throw new Error(`BILLA page ${page} is incomplete`);
  }
  return data;
}

const first = await readPage(0);
const seen = new Set();
const products = [];
for (let page = 0; page < Math.ceil(first.total / pageSize); page++) {
  const data = page === 0 ? first : await readPage(page);
  if (data.total !== first.total) throw new Error('BILLA catalog changed during refresh');
  for (const row of data.results) {
    if (typeof row.sku !== 'string' || typeof row.name !== 'string' || seen.has(row.sku)) {
      throw new Error('Invalid or duplicate BILLA article');
    }
    seen.add(row.sku);
    const {regular, standard} = row.price ?? {};
    if (!Number.isInteger(regular?.value) || regular.value <= 0) continue;
    products.push({
      sku: row.sku,
      name: row.name,
      amount: row.amount,
      volumeLabelShort: row.volumeLabelShort,
      ...(row.weightArticle === true && {weightArticle: true}),
      ...(row.inPromotion === true && {inPromotion: true}),
      price: {
        ...(row.weightArticle === true && {
          baseUnitShort: row.price.baseUnitShort,
          basePriceFactor: row.price.basePriceFactor,
        }),
        regular: {
          value: regular.value,
          ...(row.weightArticle === true && {
            perStandardizedQuantity: regular.perStandardizedQuantity,
          }),
          promotionQuantity: regular.promotionQuantity,
          promotionText: regular.promotionText,
          promotionType: regular.promotionType,
        },
        ...(Number.isInteger(standard?.value) && {
          standard: {
            value: standard.value,
            ...(row.weightArticle === true && {
              perStandardizedQuantity: standard.perStandardizedQuantity,
            }),
          },
        }),
      },
    });
  }
}
if (products.length < first.total * 0.9) {
  throw new Error(`BILLA returned prices for only ${products.length} of ${first.total} articles`);
}

await mkdir(dirname(output), {recursive: true});
await writeFile(output + '.tmp', JSON.stringify({checkedAt, products}) + '\n');
await rename(output + '.tmp', output);
console.log(`Collected BILLA prices for ${products.length} of ${first.total} articles`);

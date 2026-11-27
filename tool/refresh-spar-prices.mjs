import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const output = process.argv[2] ?? 'release/spar-prices.json';
const catalogPath = process.argv[3] ?? 'tool/spar-price-articles.json';
const catalog = JSON.parse(await readFile(catalogPath, 'utf8'));
const endpoint = 'https://api-scp.spar-ics.com/ecom/pw/v1/search/v1/search';
const stores = {spar: '5326', interspar: '8900'};
const checkedAt = new Date().toISOString();
const dateFormat = new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Vienna'});
const day = dateFormat.format(new Date(checkedAt));
const chunkSize = 32;
const products = {spar: {}, interspar: {}};
const catalogKilo = new Set(catalog.byKilo ?? []);
const produceKilo = new Set(catalog.produceByKilo ?? []);
const verifiedKilo = JSON.parse(
  await readFile(new URL('./spar-verified-kilo.json', import.meta.url), 'utf8'),
);
const perKiloLabel = /\b(?:per|pro|je)\s+kg\b/i;
let requestGap = 750;

function cents(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  const result = Math.round(value * 100);
  return result > 0 && Math.abs(value * 100 - result) < 0.0001 ? result : null;
}

function quote(hit, market) {
  const values = hit.masterValues;
  const geo = values.geoInformation?.find((entry) => entry.market === market)?.geoValues;
  const imported = Date.parse(geo?.last_imported_price);
  if (!Number.isFinite(imported) || dateFormat.format(imported) !== day) return null;
  const base = cents(geo.basePrice);
  if (base == null) return null;

  const current = cents(geo.calculatedPrice);
  const previous = cents(geo.stattPrice);
  const available = geo.available === true;
  const multi = /^Mengenvorteil ab (\d+) Stk\.? je$/i.exec(geo.promotionBadgeText ?? '');
  const minimum = Number(multi?.[1]);
  const explicitKilo = perKiloLabel.test(values.name2 ?? '');
  const confirmedUnit = verifiedKilo[values.productId];
  const currentName = [values.name1, values.name2].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  const manuallyVerifiedKilo = confirmedUnit?.slug === values.slug &&
    confirmedUnit?.name === currentName;
  const variableWeight = explicitKilo || manuallyVerifiedKilo || catalogKilo.has(values.productId) ||
    /\bca\.?\s*\d+(?:[.,]\d+)?\s*(?:g|kg)\b|\bper\s+Pkg\b/i.test(values.name2 ?? '');
  const comparisonPerKilo = geo.comparisonPrice_unit?.toLowerCase() === 'kg' &&
    geo.comparisonPrice_quantity === 1 ? cents(geo.comparisonPrice_price) : null;
  const perKilo = variableWeight && current === base
    ? comparisonPerKilo ?? (manuallyVerifiedKilo || explicitKilo && produceKilo.has(values.productId) ? base : null)
    : null;
  const price = {
    cents: perKilo ?? base,
    ...(values.name3 && {pack: values.name3}),
    ...(!available && {unavailable: true}),
    ...(perKilo && {weightBasis: 'kg'}),
  };
  if (available && !geo.hasAppVoucher && geo.mengenRabatt && minimum > 1 &&
      current != null && current < base && !variableWeight) {
    return {...price, saleCents: current, originalCents: base, minimum};
  }
  if (available && !geo.hasAppVoucher && !geo.mengenRabatt &&
      geo.hasAktionsPrice && current === base && previous != null && previous > base &&
      (perKilo == null || perKilo === base)) {
    return {...price, saleCents: current, originalCents: previous, minimum: 1};
  }
  return price;
}

async function fetchBatch(retailer, ids) {
  const market = stores[retailer];
  const url = new URL(endpoint);
  url.search = new URLSearchParams({
    query: '*', page: 1, marketId: market, hitsPerPage: chunkSize, useAsn: false,
  });
  for (const id of ids) url.searchParams.append('filter', 'productId:' + id);

  let data;
  for (let attempt = 0; ; attempt++) {
    try {
      await delay(requestGap);
      const response = await fetch(url, {signal: AbortSignal.timeout(15000)});
      if (response.status === 429) {
        const header = response.headers.get('retry-after');
        const seconds = Number(header);
        const wait = header == null ? 30000 : Number.isFinite(seconds)
          ? seconds * 1000
          : Date.parse(header) - Date.now();
        requestGap = Math.min(6000, requestGap * 2);
        const cooldown = Math.max(1000, Number.isFinite(wait) ? wait : 30000);
        console.log(`Produktwelt asked to slow down. Waiting ${Math.ceil(cooldown / 1000)} seconds.`);
        await delay(cooldown);
        throw new Error('HTTP 429');
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      if (data.timedOut || !Array.isArray(data.hits) || data.totalHits !== data.hits.length) {
        if (data.timedOut || ids.length === 1) {
          throw new Error('Incomplete Produktwelt results');
        }
        const half = Math.ceil(ids.length / 2);
        await fetchBatch(retailer, ids.slice(0, half));
        await fetchBatch(retailer, ids.slice(half));
        return;
      }
      break;
    } catch (error) {
      if (attempt === 3) throw new Error(`${retailer} batch failed: ${error.message}`);
      await delay(1000 * (attempt + 1));
    }
  }
  const expected = new Set(ids);
  for (const hit of data.hits) {
    const id = hit.masterValues?.productId;
    if (!expected.delete(id)) throw new Error(`Unexpected or duplicate ${retailer} article`);
    const price = quote(hit, market);
    if (price != null) products[retailer][id] = price;
  }
}

const batches = [];
for (const retailer of Object.keys(stores)) {
  const ids = catalog[retailer];
  if (!Array.isArray(ids) || !ids.length ||
      new Set(ids).size !== ids.length || ids.some((id) => !/^\d+$/.test(id))) {
    throw new Error(`Invalid ${retailer} article list`);
  }
  for (let start = 0; start < ids.length; start += chunkSize) {
    batches.push({retailer, ids: ids.slice(start, start + chunkSize)});
  }
}

for (const [index, {retailer, ids}] of batches.entries()) {
  await fetchBatch(retailer, ids);
  if ((index + 1) % 50 === 0) {
    console.log(`Checked ${index + 1} of ${batches.length} SPAR batches`);
  }
}

for (const retailer of Object.keys(stores)) {
  const count = Object.keys(products[retailer]).length;
  if (count < catalog[retailer].length * 0.5) {
    throw new Error(`Produktwelt returned current prices for only ${count} ${retailer} articles`);
  }
  console.log(`${retailer}: verified ${count} of ${catalog[retailer].length} article prices`);
}
await mkdir(dirname(output), {recursive: true});
await writeFile(output + '.tmp', JSON.stringify({checkedAt, products}) + '\n');
await rename(output + '.tmp', output);

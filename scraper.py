from httpx import AsyncClient, Client
from selectolax.parser import HTMLParser
from dataclasses import dataclass
import os
import asyncio
import duckdb
import json
import logging
import re
from html import escape
import math
import pandas as pd
import csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class TemuScraper:
	base_url: str = 'https://freddotoys.com'
	user_agent: str = 'Mozilla/5.0 (X11; Linux x86_64)'

	def clean_html(self, html_content):
		# 1. Remove non-standard attributes that Shopify may not recognize
		cleaned_html = re.sub(r'\sdata-[\w-]+="[^"]*"', '', html_content)

		# 2. Encode special characters like apostrophes, quotes, etc.
		cleaned_html = escape(cleaned_html)

		# 3. Decode standard HTML entities back to their original form (e.g., <, >)
		cleaned_html = cleaned_html.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')

		# 4. Remove excessive whitespace between tags
		cleaned_html = re.sub(r'>\s+<', '><', cleaned_html)

		# 5. Ensure spaces between inline elements where necessary
		cleaned_html = re.sub(r'(<span[^>]*>)\s*(<)', r'\1 <', cleaned_html)

		# 6. Remove excess spaces and newlines in text nodes
		cleaned_html = re.sub(r'\s*\n\s*', '', cleaned_html)

		return cleaned_html

	def get_product_count(self, url):
		headers = {
			'user-agent': self.user_agent
		}

		with Client(headers=headers) as client:
			response = client.get(url)
			response.raise_for_status()

		tree = HTMLParser(response.text)
		product_count = int(tree.css_first('div#ShopProductCount').text(strip=True).split()[0])

		return product_count

	async def fetch(self, aclient, url, limit):
		logger.info(f'Fetching {url}...')
		async with limit:
			response = await aclient.get(url, follow_redirects=True)
			if limit.locked():
				await asyncio.sleep(1)
				response.raise_for_status()
		logger.info(f'Fetching {url}...Completed!')

		return url, response.text

	async def fetch_all(self, urls):
		tasks = []
		headers = {
			'user-agent': self.user_agent
		}
		limit = asyncio.Semaphore(4)
		async with AsyncClient(headers=headers, timeout=120) as aclient:
			for url in urls:
				task = asyncio.create_task(self.fetch(aclient, url=url, limit=limit))
				tasks.append(task)
			htmls = await asyncio.gather(*tasks)

		return htmls

	def insert_to_db(self, htmls, database_name, table_name):
		logger.info('Inserting data to database...')
		# if os.path.exists(database_name):
		# 	os.remove(database_name)

		conn = duckdb.connect(database_name)
		curr = conn.cursor()

		try:
			curr.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (url TEXT, html BLOB)")

			htmls = [(url, bytes(html, 'utf-8') if not isinstance(html, bytes) else html) for url, html in htmls]
			curr.executemany(f"INSERT INTO {table_name} (url, html) VALUES (?, ?)", htmls)
			conn.commit()

		finally:
			curr.close()
			conn.close()
			logger.info('Data inserted!')

	def get_data(self):
		logger.info('Getting data from database...')
		conn = duckdb.connect("freddotoys.db")
		curr = conn.cursor()
		curr.execute("SELECT url, html FROM  product_src")
		datas = curr.fetchall()
		product_datas = list()

		with open('shopify_schema.json', 'r') as file:
			product_schema = json.load(file)

		for data in datas:
			current_product = product_schema.copy()
			tree = HTMLParser(data[1])

			script_tags = tree.css('script')
			script_content = None

			for script in script_tags:
				if 'window.hulkapps.product' in script.text():
					script_content = script.text()
					break
			if script_content:
				product_data_match = re.search(r'window\.hulkapps\.product\s*=\s*({.+})', script_content, re.DOTALL)
				if product_data_match:
					product_data_str = product_data_match.group(1)
					product_data = json.loads(product_data_str)

			current_product['Handle'] = product_data['handle']
			current_product['Title'] = product_data['title']
			current_product['Body (HTML)'] = product_data['description']
			current_product['Vendor'] = 'FTOYS'
			breadcrumbs = tree.css_first('div.product-breadcrumbs').text(strip=True).split('/')
			current_product['Product Category'] = ' > '.join(breadcrumbs[1:-1])
			current_product['Type'] = product_data['type']
			current_product['Tags'] = ', '.join(product_data['tags'])
			product_elem = tree.css_first('product-info > div.product__info')
			# print(product_elem.html)
			option_labels = product_elem.css('div.product-variant-picker__option-label > span.heading-font-family')
			for index, option_label in enumerate(option_labels, 1):
				current_product[f'Option{index} Name'] = option_label.text(strip=True).split(':')[0]

			option1_values = list()
			option2_values = list()
			option3_values = list()
			variant_skus = list()
			variant_weight = list()
			variant_qty = list()
			variant_cost = list()
			variant_image = list()
			variant_requires_shipping = list()
			variant_taxable = list()

			for variant in product_data['variants']:
				if current_product['Option1 Name'] != '':
					if variant['option1'] != 'None':
						option1_values.append(variant['option1'])
				else:
					option1_values = ''

				if current_product['Option2 Name'] != '':
					if variant['option2'] != 'None':
						option2_values.append(variant['option2'])
				else:
					option2_values = ''

				if current_product['Option3 Name'] != '':
					if variant['option3'] != 'None':
						option3_values.append(variant['option3'])
				else:
					option3_values = ''

				variant_skus.append(variant['sku'])
				variant_weight.append(round(variant['weight'] / 100, 2))
				variant_qty.append(10 if variant['available'] else 0)
				variant_cost.append(round(variant['price'] / 100, 2))
				try:
					variant_image.append(f"https:{variant['featured_image']['src']}")
				except Exception:
					variant_image.append('')
				variant_requires_shipping.append(variant['requires_shipping'])
				variant_taxable.append(variant['taxable'])

			current_product['Option1 Value'] = option1_values
			current_product['Option2 Value'] = option2_values
			current_product['Option3 Value'] = option3_values
			current_product['Variant SKU'] = variant_skus
			current_product['Variant Grams'] = variant_weight
			current_product['Variant Inventory Qty'] = variant_qty
			current_product['Google Shopping / Custom Label 0'] = 'FTOYS'
			current_product['Variant Image'] = variant_image
			current_product['Cost per item'] = variant_cost
			current_product['Variant Price'] = [self.get_price(x) for x in variant_cost]
			current_product['Variant Compare At Price'] = ''
			current_product['Variant Requires Shipping'] = variant_requires_shipping
			current_product['Variant Taxable'] = variant_taxable
			current_product['Image Src'] = [f'https:{url}' for url in product_data['images']]
			current_product['Image Alt Text'] = [url.split('/')[-1].split('?')[0] for url in product_data['images']]

			product_datas.append(current_product)

		df = pd.DataFrame.from_records(product_datas)

		logger.info('Data Extracted!')

		return df

	def transform_product_datas(self, df):
		df = df.explode([
			'Option1 Value', 'Variant SKU', 'Variant Grams', 'Variant Inventory Qty',
			'Variant Price', 'Variant Requires Shipping', 'Variant Taxable', 'Variant Image',
			'Cost per item'],
			ignore_index=True)
		with open('variant_unused_columns.csv', 'r') as file:
			rows = csv.reader(file)
			variant_unused_columns = [row[0] for row in rows]
		df.loc[df.duplicated('Handle', keep='first'), variant_unused_columns] = ''

		df = df.explode(['Image Src', 'Image Alt Text'], ignore_index=True)
		with open('images_unused_columns.csv', 'r') as file:
			rows = csv.reader(file)
			images_unused_columns = [row[0] for row in rows]
		df.loc[df.duplicated('Variant SKU', keep='first'), images_unused_columns] = ''
		df.drop(columns=['Variants', 'Battery Option Value', 'Battery Price'])

		return df

	def fetch_search_result_html(self, url):
		product_count = self.get_product_count(url)
		total_pages = math.ceil(product_count / 16)

		urls = [f'{url}?page={page}' for page in range(1, total_pages + 1)]
		search_results_html = asyncio.run(self.fetch_all(urls))
		self.insert_to_db(search_results_html, database_name='freddotoys.db', table_name='search_src')

	def get_product_urls(self):
		logger.info('Getting data from database...')
		conn = duckdb.connect("freddotoys.db")
		curr = conn.cursor()
		curr.execute("SELECT url, html FROM  search_src")
		datas = curr.fetchall()
		results = list()

		for data in datas:
			tree = HTMLParser(data[1])
			product_elems = tree.css('a.product-card__title')
			product_urls = list()
			for elem in product_elems:
				product_urls.append(f"{self.base_url}{elem.attributes.get('href')}")
			results.extend(product_urls)

		return results

	def fetch_product_html(self, urls):
		product_htmls = asyncio.run(self.fetch_all(urls))
		self.insert_to_db(product_htmls, database_name='freddotoys.db', table_name='product_src')

	def create_csv(self, df, csv_path):
		logger.info("Write data into csv...")
		df.to_csv(csv_path, index=False)
		logger.info("Done")
from re import search
from time import sleep
from bs4 import BeautifulSoup
from urllib.parse import unquote
from random import choice, uniform
from selenium.webdriver import Chrome
from typing import Optional, Union, List
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from ..db.redis import RedisDB
from .helper_functions import retry
from ..lib.ml_model.predict_deal import predict_deal
from ..lib.types import Websites, ProductCategories, ProductKey, Product
from ..constants.css_selectors import NEXT_BUTTON, PRODUCT_CONTAINER, PRODUCT_DETAILS, PRODUCT_CARDS
from ..constants.product import FLIPKART_AFFILIATE_ID, AMAZON_AFFILIATE_ID, MYNTRA_AFFILIATE_ID, REQUIRED_PRODUCT_KEYS, PRICE_LIMITS, MAX_PRODUCTS_PER_WEBSITE


def extract_amazon_product_id(url) -> str | None:
    decoded_url = unquote(url)
    pattern = r'/dp/([a-zA-Z0-9]{10})'

    match = search(pattern, decoded_url)

    if match:
        return match.group(1)
    else:
        return None


class SeleniumHelper:
    def __init__(self, redis: RedisDB):
        """
            Initialize the WebDriver with Chrome options and navigate to the URL
            """
        self.redis: RedisDB = redis
        self.website_name: Websites | None = None
        self.category: ProductCategories | None = None
        self.page_conter: int = 1
        self.product_urls = []

        chrome_options = Options()
        # chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument(
            "--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--disable-extensions")

        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36 Edg/92.0.902.55"
        ]
        chrome_options.add_argument(f"user-agent={choice(user_agents)}")

        self.driver = Chrome(options=chrome_options)
        self.driver.set_window_size(1920, 1080)

    def get_products(self, website_name: Websites, category: ProductCategories, url: str, ) -> List[Product] | None:
        """
        Get products details from a given URL and return a list of Product objects.

        args:
            url (str): The URL to scrape products from.

        return:
            List[Product] | None: A list of Product objects or None if no products found.
        """
        self.website_name = website_name
        self.category = category
        self.page_conter = 1
        self.product_urls = []
        products: List[Product] = []

        while len(products) >= MAX_PRODUCTS_PER_WEBSITE:
            try:
                main_container = self.fetch_product_container(url, "container")

                if main_container is None:
                    break

                extracted_products = self.extract_products_by_website(
                    main_container)
                products.extend(extracted_products)

                if not self.has_next_page():
                    break

                self.go_to_next_page()
                self.page_conter += 1

            except Exception as e:
                raise Exception(
                    f"Error occurred while getting products: {str(e)}")

        return products

    def extract_products_by_website(self, main_container: BeautifulSoup) -> List[Product]:
        """
        Extract product details from the main container based on the website.

        args:
            main_container (BeautifulSoup): The main container element containing product cards.

        return:
            List[Product]: A list of Product objects extracted from the main container.
        """
        products: List[Product] = []
        product_cards = main_container.select(
            PRODUCT_CARDS[self.website_name])

        if not product_cards:
            return []

        for product_soup in product_cards:
            product_details: Product = {}
            product_url = product_soup.select_one(" ,".join(
                PRODUCT_DETAILS[self.website_name]['product_url']))
            product_price = product_soup.select_one(
                " ,".join(PRODUCT_DETAILS[self.website_name]["product_price"]))

            if not self.is_product_valid(product_price, product_url):
                continue

            flipkart_product_soup = self.get_flipkart_data(product_url)

            if flipkart_product_soup == None:
                continue

            for key, selectors in PRODUCT_DETAILS[self.website_name].items():
                if self.website_name == Websites.FLIPKART:
                    elements = flipkart_product_soup.select_one(
                        " ,".join(selectors))
                else:
                    elements = product_soup.select_one(" ,".join(selectors))

                if elements == None and not (self.website_name == Websites.FLIPKART and key == "product_url"):
                    continue

                if key == "product_price" or key == "product_discount":
                    price_data = elements.get_text(strip=True)
                    product_details[key] = self.format_extracted_data(
                        key, price_data)

                elif key == "product_image":
                    product_details[key] = elements["src"]

                elif key == "product_rating":
                    rating_text = elements.get_text(strip=True)
                    product_details[key] = self.format_extracted_data(
                        key, rating_text)

                elif key == "product_url":
                    product_details[key] = self.format_extracted_data(
                        "product_url", product_url["href"])

                else:
                    product_details[key] = elements.get_text(strip=True)

            if None in [product_details.get(key) for key in REQUIRED_PRODUCT_KEYS]:
                continue

            prediction = predict_deal(product_details)
            if prediction['prediction'] == "Best Deal":
                product_url = self.format_extracted_data(
                    "product_url", product_url["href"])

                self.product_urls.append(product_url)
                products.append(product_details)
            else:
                continue

        return products

    def get_flipkart_data(self, url: Optional[str] = None) -> BeautifulSoup | None:
        """
        Get the main container for Flipkart products to get the product details efficiently.

        args:
            url (str): The URL to scrape products from.

        return:
            BeautifulSoup | None: The main container element containing product cards.
        """
        if self.website_name != Websites.FLIPKART or url is None:
            return None

        url = f"https://www.flipkart.com{url["href"].split("?")[0]}"

        main_container = self.fetch_product_container(url, "product")
        if main_container is None:
            return None

        return main_container

    @retry(3)
    def fetch_product_container(self, url: str, type) -> BeautifulSoup:
        """
        Fetches and returns the main product container from the specified website URL.

        args:
            url (str): The URL to scrape products from.

        return:
            BeautifulSoup | None: The main container element containing product cards.
        """
        if self.current_page <= 1 or type == "product":
            self.driver.get(url)

        WebDriverWait(self.driver, 10).until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, " ".join([PRODUCT_CONTAINER[self.website_name], PRODUCT_CARDS[self.website_name]]))))

        sleep(uniform(1.5, 2.5))

        html = self.driver.page_source

        if html is None:
            raise Exception("Failed to retrieve HTML page content.")

        soup = BeautifulSoup(html, 'html.parser')
        main_container = soup.select_one(PRODUCT_CONTAINER[self.website_name])

        return main_container

    def format_extracted_data(self, selector: ProductKey, element_data: str) -> Union[float, int, None]:
        """
        Format the extracted data based on the selector type.

        args:
            selector (Product_key): The type of data to format.
            element_data (str): The extracted data to format.

        return:
            Union[float, int, None]: The formatted data.
        """
        if element_data is None:
            return None

        if selector == "product_url":
            return self.short_url_with_affiliate_code(element_data)

        match self.website_name:
            case Websites.AMAZON:
                if selector == "product_price" or selector == "product_discount":
                    return int(float(element_data.replace(",", "").replace("₹", "")))

                elif selector == "product_rating":
                    return float(element_data.split(" ")[0])

            case Websites.FLIPKART:
                if selector == "product_price" or selector == "product_discount":
                    return int(float(element_data.replace(",", "").replace("₹", "")))

                elif selector == "product_rating":
                    return float(element_data)

            case Websites.MYNTRA:
                if selector == "product_price" or selector == "product_discount":
                    return int(float(element_data.replace("Rs.", "").replace(",", "")))

                elif selector == "product_rating":
                    return float(element_data)

    def short_url_with_affiliate_code(self, url: str) -> str:
        """
        Generate a short URL with the affiliate code based on the website.

        args:
            url (str): The original URL to shorten.

        return:
            str: The short URL with the affiliate code.
        """
        match self.website_name:
            case Websites.AMAZON:
                product_id = extract_amazon_product_id(url)
                short_url = f"https://www.amazon.in/dp/{product_id}/{AMAZON_AFFILIATE_ID}" if product_id is not None else None
                return short_url

            case Websites.FLIPKART:
                short_url = f"https://www.flipkart.com{url.split("?")[0]}/{FLIPKART_AFFILIATE_ID}"
                return short_url

            case Websites.MYNTRA:
                return f"https://www.myntra.com/{url}/{MYNTRA_AFFILIATE_ID}"

            case _:
                raise ValueError("Invalid website specified")

    def is_product_valid(self, price: BeautifulSoup, url: BeautifulSoup) -> bool:
        if price is None or url is None:
            return False

        price = price.get_text(strip=True)
        url = url["href"]

        product_price = self.format_extracted_data("product_price", price)
        product_url = self.format_extracted_data("product_url", url)

        if product_url in self.product_urls:
            return False

        if product_price <= PRICE_LIMITS[self.category]:
            # Checking if the product is already in the cache or not
            PRODUCT_CACHE_KEY = f"product_url_cache"

            if self.redis.is_member(PRODUCT_CACHE_KEY, product_url):
                return False
            else:
                return True
        else:
            return False

    def go_to_next_page(self):
        current_url = self.driver.current_url

        self.driver.find_element(
            By.CSS_SELECTOR, NEXT_BUTTON[self.website_name]).click()

        WebDriverWait(self.driver, 10).until(
            lambda d: d.current_url != current_url)

    def has_next_page():
        try:
            pass
        except:
            pass

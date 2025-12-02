import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import random
import time
import re
import threading
import json
from collections import Counter
import os
from pytrends.request import TrendReq # For Google Trends

# --- CONFIGURATION ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

# --- SECURE API KEY ---
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except (AttributeError, KeyError):
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", None)

CATEGORIES = {
    "All Departments": "aps",
    "Electronics": "electronics",
    "Computers": "computers",
    "Home & Kitchen": "kitchen",
    "Sports & Outdoors": "sports",
    "DIY & Tools": "diy",
    "Toys & Games": "toys",
    "Beauty": "beauty",
    "Books": "stripbooks",
    "Office Products": "office-products"
}

# --- PROXY MANAGER ---
class SmartProxyManager:
    def __init__(self):
        self.proxies = []
        self.current_proxy = None
        self.usage_count = 0
        self.lock = threading.Lock()
        
    def fetch_free_proxies(self):
        try:
            sources = ["https://free-proxy-list.net/", "https://www.sslproxies.org/"]
            found = []
            for source in sources:
                try:
                    resp = requests.get(source, timeout=5)
                    soup = BeautifulSoup(resp.content, "html.parser")
                    rows = soup.select("table tbody tr")
                    for row in rows:
                        cols = row.find_all("td")
                        if len(cols) >= 6 and cols[6].text == "yes":
                            found.append(f"http://{cols[0].text}:{cols[1].text}")
                except: continue
            self.proxies = list(set(found))
            random.shuffle(self.proxies)
        except: pass
        return len(self.proxies)

    def get_proxy(self):
        with self.lock:
            if not self.proxies or self.usage_count >= 5:
                if not self.proxies: self.fetch_free_proxies()
                if self.proxies:
                    self.current_proxy = self.proxies.pop(0)
                    self.usage_count = 0
            return {"http": self.current_proxy, "https": self.current_proxy} if self.current_proxy else None

    def mark_success(self):
        self.usage_count += 1
    
    def mark_failure(self):
        self.usage_count = 999 

if 'proxy_manager' not in st.session_state:
    st.session_state.proxy_manager = SmartProxyManager()

# --- ROBUST REQUEST ENGINE ---
def robust_request(url, params=None):
    pm = st.session_state.proxy_manager
    for _ in range(5):
        proxy = pm.get_proxy()
        try:
            headers = {
                "User-Agent": random.choice(USER_AGENTS), 
                "Accept-Language": "en-GB,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            response = requests.get(url, headers=headers, params=params, proxies=proxy, timeout=8)
            if response.status_code == 200:
                if "captcha" in response.text.lower():
                    pm.mark_failure()
                    continue
                pm.mark_success()
                return response
            else:
                pm.mark_failure()
        except:
            pm.mark_failure()
    return None

# --- PARSING HELPERS ---
def parse_price(price_str):
    if not price_str: return 0.0
    clean = re.sub(r'[^\d.]', '', str(price_str))
    try: return float(clean)
    except: return 0.0

def find_reviews(item_soup):
    try:
        rev_span = item_soup.select_one(".a-size-mini.s-underline-text")
        if rev_span:
            match = re.search(r"\(([\d,]+)\)", rev_span.text)
            if match: return float(match.group(1).replace(',', ''))
    except: pass
    try:
        stars = item_soup.select_one("i[data-hook='ayar-icon-service-stars']")
        if stars:
            link_sibling = stars.parent.find_next_sibling("a")
            if link_sibling and link_sibling.get('aria-label'):
                val = parse_price(link_sibling['aria-label'])
                if val > 0: return val
    except: pass
    try:
        text_content = item_soup.get_text()
        parens_match = re.search(r'\(([\d,]+)\)', text_content)
        if parens_match: return float(parens_match.group(1).replace(',', ''))
    except: pass
    return 0.0

def find_price(item_soup):
    try:
        price_whole = item_soup.select_one(".a-price-whole")
        price_fraction = item_soup.select_one(".a-price-fraction")
        if price_whole:
            whole_text = price_whole.text.strip().replace('.', '').replace(',', '')
            fraction_text = "00"
            if price_fraction: fraction_text = price_fraction.text.strip()
            return float(f"{whole_text}.{fraction_text}")
    except: pass
    return 0.0

def find_past_month_sales(item_soup):
    try:
        spans = item_soup.select("span.a-size-base.a-color-secondary")
        for span in spans:
            if "bought in past month" in span.text:
                text = span.text.strip()
                multiplier = 1
                if 'K' in text: multiplier = 1000
                if 'M' in text: multiplier = 1000000
                num_match = re.search(r'([\d\.]+)', text)
                if num_match: return float(num_match.group(1)) * multiplier
    except: pass
    return 0.0

# --- UK COMPARISON ---
def check_uk_market(asin):
    if not asin: return None
    url = f"https://www.amazon.co.uk/dp/{asin}"
    resp = robust_request(url)
    if not resp: return None
    soup = BeautifulSoup(resp.content, "html.parser")
    price_uk = 0.0
    try:
        price_block = soup.select_one("#corePrice_feature_div .a-offscreen")
        if not price_block:
            price_block = soup.select_one("#priceblock_ourprice")
        if price_block:
            price_uk = parse_price(price_block.text.replace('£', ''))
    except: pass
    return {"Price UK (£)": price_uk}

# --- NEW FEATURES LOGIC ---

def calculate_lqs(title, reviews, rating, images_est):
    """Listing Quality Score (0-10)"""
    score = 0
    # 1. Title Length (Longer is usually better SEO)
    if len(title) > 150: score += 3
    elif len(title) > 80: score += 2
    
    # 2. Social Proof
    if reviews > 50: score += 2
    if rating >= 4.0: score += 2
    
    # 3. Media (Hard to detect on search page, assuming 1 if exists)
    # We use a placeholder here as we can't count images without deep scan
    score += 2 
    
    # 4. Amazon Choice/Best Seller? (Bonus)
    # Passed implicitly via high ratings
    
    return min(score, 10)

def get_google_trends_data(keyword):
    """Fetches trend data for Ireland (IE)"""
    try:
        pytrends = TrendReq(hl='en-GB', tz=0)
        kw_list = [keyword]
        # geo='IE' for Ireland
        pytrends.build_payload(kw_list, cat=0, timeframe='today 12-m', geo='IE', gprop='')
        data = pytrends.interest_over_time()
        if not data.empty:
            return data
        return None
    except:
        return None

def calculate_profit(selling_price, landed_cost, weight_est="Standard"):
    """
    Estimates Net Profit.
    IE VAT: 23%
    Referral: ~15.3% (includes VAT on fee)
    FBA: Estimated based on tier
    """
    if selling_price == 0: return 0, 0
    
    # 1. VAT (23% included in price)
    # Price = Base * 1.23  => Base = Price / 1.23
    # VAT = Price - Base
    vat_amount = selling_price - (selling_price / 1.23)
    
    # 2. Referral Fee (15% of Gross)
    referral_fee = selling_price * 0.15
    
    # 3. FBA Fee Estimate (Rough Tiering)
    fba_fee = 3.50 # Small parcel default
    if selling_price > 25: fba_fee = 5.50
    if selling_price > 50: fba_fee = 8.00
    
    total_fees = vat_amount + referral_fee + fba_fee
    net_profit = selling_price - total_fees - landed_cost
    margin = (net_profit / selling_price) * 100
    
    return net_profit, margin

# --- ANALYTICS ---
def calculate_opportunity(row):
    try:
        price = row['Price']
        reviews = row['Reviews']
        revenue = row['Est. Monthly Revenue']
        if price == 0: return 0
        score = 0
        if 20 <= price <= 100: score += 30
        elif price > 15: score += 15
        if reviews < 50: score += 40
        elif reviews < 150: score += 25
        elif reviews < 500: score += 10
        if revenue > 5000: score += 30
        elif revenue > 1000: score += 15
        elif revenue > 500: score += 5
        return min(int(score), 100)
    except: return 0

def estimate_revenue(price, past_sales, reviews):
    if past_sales > 0: return past_sales * price
    return (reviews * 1.5) * price

# --- GEMINI AI ---
def run_gemini_analysis(api_key, df, keyword, market_context="IE"):
    if not api_key: return "🤖 AI Disabled (No Key)"
    try:
        top_5 = df.head(5).to_dict('records')
        avg_price = df['Price'].mean()
        
        prompt_text = f"""
        Act as a Lead E-Commerce Strategist for Amazon Europe (Ireland focus).
        Niche: '{keyword}'.
        
        Data Snapshot:
        - Avg Price: €{avg_price:.2f}
        - Top Products: {top_5}
        
        Provide a "Voice of the Customer" analysis:
        1. **Pain Points:** Based on the product titles and ratings, what are customers likely complaining about? (e.g. "Flimsy", "Too small").
        2. **The "Golden Angle":** What specific feature is missing from the top 5 that I should add?
        3. **Launch Verdict:** Go or No Go?
        """
        
        model = "gemini-2.0-flash"
        headers = {'Content-Type': 'application/json'}
        data = {"contents": [{"parts": [{"text": prompt_text}]}]}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        if resp.status_code == 200:
            return f"**Analysis by {model}:**\n\n" + resp.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            return f"AI Error: {resp.text}"
    except Exception as e: return f"Error: {str(e)}"

# --- MAIN APP ---
st.set_page_config(page_title="Amazon.ie Niche Command", layout="wide", page_icon="🍀")

st.title("🍀 Amazon.ie Niche Command Center")

with st.sidebar:
    st.header("🔍 Parameters")
    search_mode = st.radio("Mode", ["Keyword", "Category"])
    if search_mode == "Keyword":
        keyword = st.text_input("Keyword", "Yoga Mat")
        category_filter = st.selectbox("Department", list(CATEGORIES.keys()))
    else:
        target_category = st.selectbox("Category", list(CATEGORIES.keys()))
        keyword = target_category
    
    st.markdown("---")
    # PROFIT CALCULATOR
    st.header("🧮 Profit Calc")
    st.caption("Estimate your margins for this niche.")
    landed_cost_input = st.number_input("Your Landed Cost (€)", value=5.00, step=0.5)
    
    st.markdown("---")
    compare_uk = st.checkbox("Compare UK Prices (Slower)")
    pages = st.slider("Scan Depth", 1, 3, 1)
    run_btn = st.button("🚀 Execute", type="primary")

if run_btn:
    if not st.session_state.proxy_manager.proxies:
        st.session_state.proxy_manager.fetch_free_proxies()

    status = st.empty()
    prog = st.progress(0)
    all_products = []

    base_url = "https://www.amazon.ie/s"
    search_params = {"page": 1}
    
    if search_mode == "Keyword":
        search_params["k"] = keyword
        search_params["i"] = CATEGORIES[category_filter]
    else:
        search_params["k"] = "*" 
        search_params["i"] = CATEGORIES[target_category]
        keyword = target_category

    for p in range(1, pages + 1):
        search_params["page"] = p
        status.info(f"Scanning Sector {p}...")
        resp = robust_request(base_url, search_params)
        
        if resp:
            soup = BeautifulSoup(resp.content, "html.parser")
            items = soup.select('div[data-component-type="s-search-result"]')
            if not items: 
                items = [i for i in soup.select('div.s-result-item[data-asin]') if i.get('data-asin')]
            
            for i, item in enumerate(items):
                try:
                    title_el = item.select_one("h2 span")
                    title = title_el.text if title_el else "Unknown"
                    asin = item.get('data-asin')
                    price = find_price(item)
                    reviews = find_reviews(item)
                    past_sales = find_past_month_sales(item)
                    
                    # Rating (Approx)
                    rating = 0.0
                    rating_tag = item.select_one("i.a-icon-star-small span")
                    if rating_tag: 
                        rating = float(rating_tag.text.split()[0])

                    link = "https://www.amazon.ie" + item.select_one("a.a-link-normal")['href'] if item.select_one("a.a-link-normal") else ""
                    
                    # Alibaba Link
                    alibaba_link = f"https://www.alibaba.com/trade/search?SearchText={keyword.replace(' ', '+')}"
                    
                    # Metrics
                    est_rev = estimate_revenue(price, past_sales, reviews)
                    lqs = calculate_lqs(title, reviews, rating, 1)
                    
                    # Profit Calc (Per product simulation)
                    net_profit, net_margin = calculate_profit(price, landed_cost_input)

                    prod = {
                        "Title": title,
                        "Price": price,
                        "Reviews": reviews,
                        "LQS": lqs,
                        "Est. Monthly Revenue": est_rev,
                        "Net Profit (€)": net_profit,
                        "Margin (%)": net_margin,
                        "Alibaba": alibaba_link,
                        "Link": link,
                        "ASIN": asin
                    }
                    
                    if compare_uk and asin:
                        status.text(f"Checking UK: {asin}...")
                        uk = check_uk_market(asin)
                        if uk:
                            prod["Price UK (£)"] = uk["Price UK (£)"]
                            prod["Arbitrage Gap (€)"] = price - (uk["Price UK (£)"] * 1.20)
                        else:
                            prod["Price UK (£)"] = 0
                            prod["Arbitrage Gap (€)"] = 0
                            
                    all_products.append(prod)
                    
                except: continue
            prog.progress(p / pages)

    if all_products:
        df = pd.DataFrame(all_products)
        df['Opp Score'] = df.apply(calculate_opportunity, axis=1)
        status.success("Analysis Complete!")
        
        # --- DOWNLOAD ---
        st.sidebar.markdown("---")
        csv = df.to_csv(index=False).encode('utf-8')
        st.sidebar.download_button("📥 Download Data", csv, "data.csv", "text/csv")

        # --- KPI ---
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Avg Price", f"€{df['Price'].mean():.2f}")
        k2.metric("Avg Net Margin", f"{df['Margin (%)'].mean():.1f}%")
        k3.metric("Market Value", f"€{df['Est. Monthly Revenue'].sum():,.0f}")
        k4.metric("Avg LQS", f"{df['LQS'].mean():.1f}/10")

        # --- TABS ---
        t1, t2, t3, t4 = st.tabs(["📋 Data", "📉 Trends (IE)", "🧠 AI & Voice", "💡 Arbitrage"])
        
        with t1:
            st.dataframe(
                df.style.background_gradient(subset=['Est. Monthly Revenue'], cmap="Greens"),
                column_config={
                    "Link": st.column_config.LinkColumn("Amazon"),
                    "Alibaba": st.column_config.LinkColumn("Source it"),
                    "LQS": st.column_config.ProgressColumn(min_value=0, max_value=10, format="%d"),
                    "Margin (%)": st.column_config.NumberColumn(format="%.1f%%"),
                    "Net Profit (€)": st.column_config.NumberColumn(format="€%.2f"),
                    "Arbitrage Gap (€)": st.column_config.NumberColumn(format="€%.2f")
                },
                use_container_width=True
            )

        with t2:
            st.subheader(f"Google Trends: {keyword} (Ireland)")
            trends_data = get_google_trends_data(keyword)
            if trends_data is not None:
                st.line_chart(trends_data)
            else:
                st.warning("Google Trends API blocked (common on cloud servers).")
                st.markdown(f"[👉 Click here to view '{keyword}' trends manually on Google](https://trends.google.com/trends/explore?geo=IE&q={keyword})")

        with t3:
            if GEMINI_API_KEY:
                st.markdown(run_gemini_analysis(GEMINI_API_KEY, df, keyword))
            else: st.error("No API Key")

        with t4:
            if compare_uk and "Arbitrage Gap (€)" in df.columns:
                arb = df[df["Arbitrage Gap (€)"] > 5]
                st.dataframe(arb[["Title", "Price", "Price UK (£)", "Arbitrage Gap (€)", "Link"]])
            else: st.info("Enable UK Compare in sidebar.")
    else:
        st.error("No products found.")

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

# --- CONFIGURATION ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

# Hardcoded API Key (Hidden from UI)
GEMINI_API_KEY = "AIzaSyDYwJ2WojuQ81W5cCXQU0DtaStq215JXEE"

# Map display names to Amazon URL aliases
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
            # Combining multiple sources for robustness
            sources = ["https://free-proxy-list.net/", "https://www.sslproxies.org/"]
            found = []
            
            for source in sources:
                try:
                    resp = requests.get(source, timeout=5)
                    soup = BeautifulSoup(resp.content, "html.parser")
                    rows = soup.select("table tbody tr")
                    for row in rows:
                        cols = row.find_all("td")
                        if len(cols) >= 6 and cols[6].text == "yes": # HTTPS check
                            found.append(f"http://{cols[0].text}:{cols[1].text}")
                except: continue

            self.proxies = list(set(found))
            random.shuffle(self.proxies)
        except:
            pass
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
    for _ in range(5): # Retries
        proxy = pm.get_proxy()
        try:
            headers = {
                "User-Agent": random.choice(USER_AGENTS), 
                "Accept-Language": "en-GB,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            response = requests.get(url, headers=headers, params=params, proxies=proxy, timeout=8)
            
            # Amazon anti-bot checks
            if response.status_code == 200:
                if "captcha" in response.text.lower():
                    pm.mark_failure()
                    continue
                if "api-services-support@amazon.com" in response.text:
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
    # Strategy 1: Targeted "Mini" text class with Parentheses (High precision)
    try:
        rev_span = item_soup.select_one(".a-size-mini.s-underline-text")
        if rev_span:
            match = re.search(r"\(([\d,]+)\)", rev_span.text)
            if match: return float(match.group(1).replace(',', ''))
    except: pass

    # Strategy 2: Aria Labels (Accessibility hidden text)
    try:
        stars = item_soup.select_one("i[data-hook='ayar-icon-service-stars']")
        if stars:
            # Look at parent's next sibling link (common in new layout)
            link_sibling = stars.parent.find_next_sibling("a")
            if link_sibling and link_sibling.get('aria-label'):
                val = parse_price(link_sibling['aria-label'])
                if val > 0: return val
    except: pass
    
    # Strategy 3: Loose Regex on text (Last resort)
    try:
        text_content = item_soup.get_text()
        # Look for (1,234) pattern
        parens_match = re.search(r'\(([\d,]+)\)', text_content)
        if parens_match:
            return float(parens_match.group(1).replace(',', ''))
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
                if num_match:
                    return float(num_match.group(1)) * multiplier
    except: pass
    return 0.0

def find_bsr_basic(item_soup):
    try:
        badge = item_soup.select_one(".a-badge-text")
        if badge and "Best Seller" in badge.text:
            return 1
    except: pass
    return 0

# --- UK COMPARISON ENGINE ---
def check_uk_market(asin):
    """Checks the same ASIN on Amazon.co.uk to compare price/stock"""
    if not asin: return None
    
    url = f"https://www.amazon.co.uk/dp/{asin}"
    resp = robust_request(url)
    
    if not resp: return None
    
    soup = BeautifulSoup(resp.content, "html.parser")
    
    # Extract UK Price
    price_uk = 0.0
    try:
        price_block = soup.select_one("#corePrice_feature_div .a-offscreen")
        if not price_block:
            price_block = soup.select_one("#priceblock_ourprice")
        if price_block:
            # GBP symbol removal
            price_uk = parse_price(price_block.text.replace('£', ''))
    except: pass
    
    return {
        "Price UK (£)": price_uk,
        "Exists on UK": True
    }

# --- ANALYTICS ---
def calculate_opportunity(row):
    try:
        price = row['Price']
        reviews = row['Reviews']
        revenue = row['Est. Monthly Revenue']
        
        if price == 0: return 0
        
        # 1. Price Score (Margin Potential)
        score = 0
        if 20 <= price <= 100: score += 30
        elif price > 15: score += 15
        
        # 2. Competition Score (Ease of Entry)
        if reviews < 50: score += 40
        elif reviews < 150: score += 25
        elif reviews < 500: score += 10
        
        # 3. Demand Score (Proof of Concept)
        if revenue > 5000: score += 30
        elif revenue > 1000: score += 15
        elif revenue > 500: score += 5
        
        return min(int(score), 100)
    except: return 0

def estimate_revenue(price, past_sales, reviews):
    # Tier 1: Hard Data
    if past_sales > 0: return past_sales * price
    # Tier 2: Review Velocity Proxy (Conservative: 1 review = 50 sales lifetime / ~12 months = ~4/mo?)
    # Helium 10 logic is complex, but a simple proxy for monthly is usually Review_Count * Multiplier
    # We will use a conservative monthly multiplier of 1.5x total reviews if no other data
    return (reviews * 1.5) * price

# --- GEMINI AI ---
def run_gemini_analysis(api_key, df, keyword, market_context="IE"):
    try:
        top_5 = df.head(5).to_dict('records')
        avg_price = df['Price'].mean()
        avg_rev = df['Reviews'].mean()
        
        prompt_text = f"""
        Act as a Lead E-Commerce Strategist for Amazon Europe.
        Target Market: Amazon {market_context} (Ireland).
        Niche: '{keyword}'.
        
        Data Snapshot:
        - Avg Price: €{avg_price:.2f}
        - Avg Competition (Reviews): {avg_rev:.0f}
        - Top Products: {top_5}
        
        Generate a Strategy Report:
        1. **Gap Analysis:** Identify features missing from the top 5 (e.g., poor images, bad titles, missing variations).
        2. **The "Irish Edge":** How can I localize this for Ireland? (e.g., "Fast Delivery to Cork/Dublin", "Local Customer Support").
        3. **Profitability Verdict:** Is this a "Go" or "No Go"? Why?
        """
        
        # Exclusively using gemini-2.0-flash as requested
        model = "gemini-2.0-flash"
        headers = {'Content-Type': 'application/json'}
        data = {"contents": [{"parts": [{"text": prompt_text}]}]}
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=15)
            if resp.status_code == 200:
                return f"**Analysis by {model}:**\n\n" + resp.json()['candidates'][0]['content']['parts'][0]['text']
            else:
                return f"AI Error ({resp.status_code}): {resp.text}"
        except Exception as e:
            return f"Connection Error: {str(e)}"
            
    except Exception as e: return f"System Error: {str(e)}"

# --- MAIN APP ---
st.set_page_config(page_title="Amazon.ie Niche Command", layout="wide", page_icon="🍀")

# Styling
st.markdown("""
<style>
    .metric-card {background-color: #f0f2f6; padding: 15px; border-radius: 10px; border-left: 5px solid #ff9900;}
    .stProgress > div > div > div > div { background-color: #00cc66; }
</style>
""", unsafe_allow_html=True)

st.title("🍀 Amazon.ie Niche Command Center")
st.markdown("Professional-grade market intelligence for the emerging Irish marketplace.")

with st.sidebar:
    st.header("🔍 Mission Parameters")
    
    search_mode = st.radio("Search Mode", ["Keyword", "Category Browsing"])
    
    if search_mode == "Keyword":
        keyword = st.text_input("Target Keyword", "Yoga Mat")
        category_filter = st.selectbox("Department Filter", list(CATEGORIES.keys()))
    else:
        # Defaults for Category Browsing
        target_category = st.selectbox("Select Category", list(CATEGORIES.keys()))
        keyword = target_category
    
    st.markdown("---")
    st.markdown("### 🇬🇧 Cross-Border Intel")
    compare_uk = st.checkbox("Compare with Amazon.co.uk", help="Checks prices on UK site to find Arbitrage Gaps. SLOWER.")
    
    st.markdown("---")
    pages = st.slider("Scan Depth (Pages)", 1, 3, 1)
    run_btn = st.button("🚀 Execute Mission", type="primary")

if run_btn:
    if not st.session_state.proxy_manager.proxies:
        st.session_state.proxy_manager.fetch_free_proxies()

    status_container = st.empty()
    progress_bar = st.progress(0)
    all_products = []

    # Construct Search URL
    base_url = "https://www.amazon.ie/s"
    search_params = {"page": 1}
    
    if search_mode == "Keyword":
        search_params["k"] = keyword
        search_params["i"] = CATEGORIES[category_filter]
    else:
        # CRITICAL FIX: Use wildcard '*' instead of empty string. 
        # k="" causes a redirect to a landing page (not a search list).
        # k="*" forces Amazon to show a list of all items in the node.
        search_params["k"] = "*" 
        search_params["i"] = CATEGORIES[target_category]
        keyword = target_category # For file naming/AI context

    for p in range(1, pages + 1):
        search_params["page"] = p
        status_container.info(f"Scanning Sector {p} of {pages}...")
        
        resp = robust_request(base_url, search_params)
        
        if resp:
            soup = BeautifulSoup(resp.content, "html.parser")
            
            # Primary Selector
            items = soup.select('div[data-component-type="s-search-result"]')
            
            # Fallback Selector (For grid layouts often used in category browsing)
            if not items:
                items = soup.select('div.s-result-item[data-asin]')
                # Filter out empty ASINs (spacers)
                items = [i for i in items if i.get('data-asin')]

            total_items_page = len(items)
            
            if total_items_page == 0:
                status_container.warning(f"Sector {p}: No items found using standard layout. Amazon may be showing a department landing page.")
            
            for i, item in enumerate(items):
                try:
                    # Basic Extraction
                    title_el = item.select_one("h2 span")
                    title = title_el.text if title_el else "Unknown Product"
                    asin = item.get('data-asin')
                    
                    price = find_price(item)
                    reviews = find_reviews(item)
                    past_sales = find_past_month_sales(item)
                    bsr_est = find_bsr_basic(item) # Only grab badge BSR to be fast
                    
                    # Link
                    link_el = item.select_one("a.a-link-normal")
                    link = "https://www.amazon.ie" + link_el['href'] if link_el else ""
                    
                    # Revenue Estimate
                    est_rev = estimate_revenue(price, past_sales, reviews)
                    
                    product_data = {
                        "ASIN": asin,
                        "Title": title,
                        "Price": price,
                        "Reviews": reviews,
                        "Past Month Sales": past_sales,
                        "Est. Monthly Revenue": est_rev,
                        "Link": link
                    }
                    
                    # UK Comparison Logic
                    if compare_uk and asin:
                        status_container.text(f"🇬🇧 Cross-checking UK: {asin}...")
                        uk_data = check_uk_market(asin)
                        if uk_data:
                            product_data["Price UK (£)"] = uk_data["Price UK (£)"]
                            # Simple FX rate 1.2
                            price_uk_eur = uk_data["Price UK (£)"] * 1.20
                            product_data["Arbitrage Gap (€)"] = price - price_uk_eur
                        else:
                            product_data["Price UK (£)"] = 0
                            product_data["Arbitrage Gap (€)"] = 0
                            
                    all_products.append(product_data)
                    
                    # Update Progress
                    prog_val = ((p-1)/pages) + ((i+1)/(total_items_page * pages)) if total_items_page > 0 else 0
                    progress_bar.progress(min(prog_val, 1.0))
                    
                except Exception as e: continue
        
    if all_products:
        df = pd.DataFrame(all_products)
        df['Opp Score'] = df.apply(calculate_opportunity, axis=1)
        
        status_container.success("Analysis Complete!")
        
        # --- NEW: SIDEBAR DOWNLOAD ---
        st.sidebar.markdown("---")
        csv = df.to_csv(index=False).encode('utf-8')
        st.sidebar.download_button(
            label="📥 Download CSV Report",
            data=csv,
            file_name=f"amazon_ie_{keyword.replace(' ', '_')}.csv",
            mime="text/csv",
            type="primary"
        )
        
        # --- DASHBOARD LAYOUT ---
        
        # 1. High Level KPIs
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        kpi1.metric("Avg Price", f"€{df['Price'].mean():.2f}")
        kpi2.metric("Total Est. Monthly Rev", f"€{df['Est. Monthly Revenue'].sum():,.0f}")
        kpi3.metric("Avg Reviews", f"{int(df['Reviews'].mean())}")
        
        if compare_uk and "Arbitrage Gap (€)" in df.columns:
            avg_gap = df[df['Arbitrage Gap (€)'] > 0]['Arbitrage Gap (€)'].mean()
            kpi4.metric("Avg Arbitrage Gap", f"€{avg_gap:.2f}" if not pd.isna(avg_gap) else "€0.00")
        else:
            kpi4.metric("Top Opp Score", f"{df['Opp Score'].max()}/100")

        # 2. Tabs
        tab_data, tab_charts, tab_ai, tab_arbitrage = st.tabs(["📋 Data Grid", "📈 Opportunity Map", "🤖 AI Strategy", "💱 Arbitrage Hunter"])
        
        with tab_data:
            # Configure columns dynamically
            cols_config = {
                "Link": st.column_config.LinkColumn(),
                "Est. Monthly Revenue": st.column_config.NumberColumn(format="€%.2f"),
                "Opp Score": st.column_config.ProgressColumn(format="%d", min_value=0, max_value=100),
                "Price": st.column_config.NumberColumn(format="€%.2f"),
                "Reviews": st.column_config.NumberColumn(format="%d")
            }
            if compare_uk:
                cols_config["Price UK (£)"] = st.column_config.NumberColumn(format="£%.2f")
                cols_config["Arbitrage Gap (€)"] = st.column_config.NumberColumn(format="€%.2f")

            st.dataframe(
                df.style.background_gradient(subset=['Est. Monthly Revenue'], cmap="Greens"),
                column_config=cols_config,
                use_container_width=True
            )

        with tab_charts:
            st.markdown("### 🔵 Opportunity Map (Price vs Reviews)")
            # Clean data for chart to prevent loading errors
            chart_df = df[['Price', 'Reviews', 'Opp Score', 'Title']].copy()
            chart_df = chart_df.dropna()
            
            st.scatter_chart(
                chart_df,
                x='Reviews',
                y='Price',
                color='Opp Score',
                size='Opp Score',
                use_container_width=True
            )
            
        with tab_ai:
            if GEMINI_API_KEY:
                st.markdown(run_gemini_analysis(GEMINI_API_KEY, df, keyword))
            else:
                st.warning("API Key not found in configuration.")

        with tab_arbitrage:
            if compare_uk and "Arbitrage Gap (€)" in df.columns:
                st.markdown("### 🇬🇧 vs 🇮🇪 Arbitrage Opportunities")
                st.caption("Products significantly cheaper in UK (Gap > €5). You could potentially FBM these.")
                
                arb_opps = df[df["Arbitrage Gap (€)"] > 5].sort_values("Arbitrage Gap (€)", ascending=False)
                
                if not arb_opps.empty:
                    st.dataframe(
                        arb_opps[["Title", "Price", "Price UK (£)", "Arbitrage Gap (€)", "Link"]],
                        column_config={
                            "Link": st.column_config.LinkColumn(),
                            "Arbitrage Gap (€)": st.column_config.NumberColumn(format="€%.2f")
                        }
                    )
                else:
                    st.info("No significant arbitrage gaps (>€5) found in this batch.")
            else:
                st.info("Enable 'Compare with Amazon.co.uk' in the sidebar to see this data.")

    else:
        st.error("Mission Failed: No products retrieved. Proxies may need cooldown or Captcha block.")
# ==============================================================================
# 1. FILE: vercel.json (Must be in root for Vercel deployment)
# {
#   "version": 2,
#   "builds": [{"src": "app.py", "use": "@vercel/python"}],
#   "routes": [{"src": "/(.*)", "dest": "app.py"}],
#   "functions": {"app.py": {"maxDuration": 60}}
# }
# ==============================================================================
# 2. FILE: requirements.txt (Must be in root)
# Flask==3.0.0
# beautifulsoup4==4.12.2
# playwright==1.40.0
# ==============================================================================
# 3. FILE: app.py
# ==============================================================================

import os
import re
import tempfile
import uuid
import logging
from urllib.parse import urljoin, urlparse

from flask import Flask, request, jsonify, send_file, render_template_string
from bs4 import BeautifulSoup

# Import Playwright for Headless Browser Execution
from playwright.sync_api import sync_playwright

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TEMP_DIR = tempfile.gettempdir()
CSS_URL_REGEX = re.compile(r'url\(\s*(["\']?)([^)]+)\1\s*\)', re.IGNORECASE)

# --- FLASK HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stealth Web Archiver</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #121212; color: #ffffff; padding-top: 40px; }
        .card { background-color: #1e1e1e; border: 1px solid #333; box-shadow: 0 4px 6px rgba(0,0,0,0.5); }
        .form-control { background-color: #2a2a2a; border: 1px solid #444; color: #fff; }
        .form-control:focus { background-color: #333; color: #fff; border-color: #0d6efd; box-shadow: none; }
        #loading-container { display: none; }
        .stealth-badge { font-size: 0.8rem; background: #dc3545; padding: 3px 8px; border-radius: 12px; margin-bottom:15px; display:inline-block; font-weight:bold;}
    </style>
</head>
<body>
<div class="container">
    <div class="row justify-content-center">
        <div class="col-md-8">
            <div class="card">
                <div class="card-header bg-primary text-white border-bottom-0">
                    <h4 class="mb-0">Anti-Bot Stealth Archiver</h4>
                </div>
                <div class="card-body">
                    <div class="stealth-badge">WAF Bypass Enabled (Akamai/Cloudflare/Datadome)</div>
                    <p class="text-muted small">
                        This tool mimics a legitimate browser, waits <strong>30 seconds</strong> for React/Angular/Vue components to render, and forces the browser to fetch assets internally to bypass 403 Forbidden firewall blocks.<br>
                    </p>
                    
                    <form id="archiveForm">
                        <div class="mb-3">
                            <label for="url" class="form-label">Target URL</label>
                            <input type="url" class="form-control" id="url" placeholder="https://www.flipkart.com" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100" id="btn-submit">Bypass Security & Archive</button>
                    </form>

                    <div id="loading-container" class="mt-4 text-center">
                        <div class="spinner-border text-primary" role="status">
                            <span class="visually-hidden">Loading...</span>
                        </div>
                        <p class="mt-2 fw-bold text-light" id="status-text">Booting Stealth Browser... waiting 30s...</p>
                        <p class="text-muted small">Do not close this tab. Complex sites take ~45-60 seconds.</p>
                    </div>
                    
                    <div id="error-box" class="alert alert-danger mt-3" style="display: none;"></div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
document.getElementById('archiveForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    const url = document.getElementById('url').value;
    const btnSubmit = document.getElementById('btn-submit');
    const loadingContainer = document.getElementById('loading-container');
    const errorBox = document.getElementById('error-box');

    btnSubmit.disabled = true;
    loadingContainer.style.display = 'block';
    errorBox.style.display = 'none';

    try {
        const response = await fetch('/api/archive', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: url})
        });
        
        if (!response.ok) {
            let errorMsg = "Server error or timeout exceeded.";
            try {
                const data = await response.json();
                if (data.error) errorMsg = data.error;
            } catch(e) {}
            throw new Error(errorMsg);
        }

        const blob = await response.blob();
        const downloadUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        
        const urlObj = new URL(url);
        const domain = urlObj.hostname.replace(/\./g, '_');
        
        a.href = downloadUrl;
        a.download = `${domain}_stealth_archive.html`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(downloadUrl);
        
        loadingContainer.style.display = 'none';
        btnSubmit.disabled = false;
        document.getElementById('url').value = '';

    } catch (err) {
        errorBox.innerText = "Error: " + err.message;
        errorBox.style.display = 'block';
        loadingContainer.style.display = 'none';
        btnSubmit.disabled = false;
    }
});
</script>
</body>
</html>
"""

# JavaScript Payload injected into Playwright to fetch assets securely
# Bypasses WAF by using the authenticated browser's own networking stack
BROWSER_FETCHER_JS = """
async (args) => {
    try {
        const resp = await fetch(args.url);
        if (!resp.ok) return { success: false, error: `HTTP ${resp.status}` };
        
        if (args.type === 'text') {
            const text = await resp.text();
            return { success: true, data: text };
        } else {
            const blob = await resp.blob();
            return await new Promise((resolve) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve({ success: true, data: reader.result });
                reader.onerror = () => resolve({ success: false, error: 'Blob conversion failed' });
                reader.readAsDataURL(blob);
            });
        }
    } catch(err) {
        return { success: false, error: err.toString() };
    }
}
"""

class StealthArchiver:
    def __init__(self, target_url):
        self.target_url = target_url
        self.final_url = target_url
        self.resource_cache = {}
        self.page = None

    def fetch_via_browser(self, url, res_type):
        """Asks the browser environment to download the resource to perfectly spoof TLS and Cookies."""
        if url.startswith('data:'): return url
        if url in self.resource_cache: return self.resource_cache[url]
        
        try:
            logger.info(f"Browser fetching: {url}")
            # Ask JS to fetch it
            result = self.page.evaluate(BROWSER_FETCHER_JS, {"url": url, "type": res_type})
            
            if result and result.get('success'):
                data = result['data']
                self.resource_cache[url] = data
                return data
            else:
                logger.warning(f"Browser fetch failed for {url}: {result.get('error')}")
                return None
        except Exception as e:
            logger.error(f"Execution error fetching {url}: {e}")
            return None

    def process_css_content(self, css_text, base_url):
        """Finds nested URLs in CSS and fetches them via the browser."""
        def replacer(match):
            quote = match.group(1)
            inner_url = match.group(2).strip()
            if inner_url.startswith('data:') or inner_url.startswith('#'):
                return match.group(0)
            
            absolute_url = urljoin(base_url, inner_url)
            data_uri = self.fetch_via_browser(absolute_url, res_type="base64")
            
            if data_uri:
                return f"url({quote}{data_uri}{quote})"
            return match.group(0)
            
        return CSS_URL_REGEX.sub(replacer, css_text)

    def process(self):
        html_content = ""
        
        with sync_playwright() as p:
            # ADVANCED TRICK 1: Anti-Detection Launch Arguments & CORS Disabling
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-web-security',              # Bypass CORS checks to allow fetching CDN assets
                    '--disable-blink-features=AutomationControlled', # Hide headless flag from Datadome/Cloudflare
                    '--no-sandbox', 
                    '--disable-setuid-sandbox'
                ]
            )
            
            # ADVANCED TRICK 2: Spoof realistic User-Agent and Viewport
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
                has_touch=False,
                is_mobile=False
            )
            
            # ADVANCED TRICK 3: Inject script to scrub webdriver properties before page loads
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """)
            
            self.page = context.new_page()
            
            try:
                logger.info(f"Navigating to {self.target_url}")
                self.page.goto(self.target_url, wait_until='domcontentloaded', timeout=40000)
            except Exception as e:
                logger.warning(f"Navigation issue (Continuing anyway): {e}")

            # Hard wait for React/JS components to load in fully
            logger.info("Waiting 30 seconds for dynamic content...")
            self.page.wait_for_timeout(30000)
            
            # Scroll down to trigger lazy-loaded images (Common in E-commerce like Flipkart)
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight/2);")
            self.page.wait_for_timeout(1000)
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            self.page.wait_for_timeout(2000)

            html_content = self.page.content()
            self.final_url = self.page.url 

            # PARSE DOM
            soup = BeautifulSoup(html_content, 'html.parser')
            base_tag = soup.find('base')
            base_url = urljoin(self.final_url, base_tag['href']) if base_tag and base_tag.has_attr('href') else self.final_url

            # COLLECT ASSETS
            images_to_fetch = []
            for img in soup.find_all(['img', 'source']):
                for attr in ['src', 'srcset', 'data-src', 'data-url']: # Added data-url for lazy loaders
                    if img.has_attr(attr) and not img[attr].startswith('data:'):
                        images_to_fetch.append((img, attr, img[attr]))

            css_to_fetch = []
            for link in soup.find_all('link', rel='stylesheet'):
                if link.has_attr('href'):
                    css_to_fetch.append((link, link['href']))

            js_to_fetch = []
            for script in soup.find_all('script', src=True):
                js_to_fetch.append((script, script['src']))

            # FETCH & REPLACE (Sequential via browser to guarantee TLS integrity)
            
            # 1. Process Images
            for tag, attr, raw_url in images_to_fetch:
                abs_url = urljoin(base_url, raw_url.split()[0]) # split()[0] handles srcset safely
                data_uri = self.fetch_via_browser(abs_url, res_type="base64")
                if data_uri:
                    if attr == 'srcset':
                        del tag['srcset']
                        tag['src'] = data_uri
                    else:
                        tag[attr] = data_uri

            # 2. Process CSS
            for tag, raw_url in css_to_fetch:
                abs_url = urljoin(base_url, raw_url)
                css_text = self.fetch_via_browser(abs_url, res_type="text")
                if css_text:
                    processed_css = self.process_css_content(css_text, abs_url)
                    style_tag = soup.new_tag('style')
                    style_tag.string = processed_css
                    tag.replace_with(style_tag)

            # 3. Process JS
            for tag, raw_url in js_to_fetch:
                abs_url = urljoin(base_url, raw_url)
                js_text = self.fetch_via_browser(abs_url, res_type="text")
                if js_text:
                    script_tag = soup.new_tag('script')
                    script_tag.string = js_text
                    if tag.has_attr('type'): script_tag['type'] = tag['type']
                    tag.replace_with(script_tag)

            # 4. Process Inline Styles
            for style in soup.find_all('style'):
                if style.string:
                    style.string = self.process_css_content(style.string, base_url)

            # Clean up security blockers
            if base_tag: base_tag.decompose()
            for meta in soup.find_all('meta', attrs={'http-equiv': lambda x: x and x.lower() == 'content-security-policy'}):
                meta.decompose()

            browser.close()
            return str(soup)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/archive', methods=['POST'])
def archive_sync():
    data = request.json
    url = data.get('url')
    
    if not url: return jsonify({'error': 'URL is required'}), 400
    
    parsed = urlparse(url)
    if parsed.scheme not in ['http', 'https']:
        return jsonify({'error': 'Invalid URL scheme. Use http or https.'}), 400

    try:
        archiver = StealthArchiver(url)
        final_html = archiver.process()
        
        job_id = str(uuid.uuid4())
        filepath = os.path.join(TEMP_DIR, f"stealth_{job_id}.html")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(final_html)
            
        domain = parsed.netloc.replace('.', '_')
        return send_file(
            filepath, 
            as_attachment=True, 
            download_name=f"{domain}_stealth_archive.html", 
            mimetype='text/html'
        )
    except Exception as e:
        logger.error(f"Archiving error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Local requirements: pip install -r requirements.txt && playwright install chromium
    app.run(debug=True)

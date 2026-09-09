import os
import re
import base64
import logging
import mimetypes
import tempfile
import uuid
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, send_file, render_template_string
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

MAX_RESOURCE_SIZE = 5 * 1024 * 1024  # 5 MB per resource limit
MAX_CONCURRENT_DOWNLOADS = 10
TEMP_DIR = tempfile.gettempdir()
CSS_URL_REGEX = re.compile(r'url\(\s*(["\']?)([^)]+)\1\s*\)', re.IGNORECASE)

# --- FLASK HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Web Archiver (Vercel Edition)</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f8f9fa; padding-top: 40px; }
        .card { box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        #loading-container { display: none; }
    </style>
</head>
<body>
<div class="container">
    <div class="row justify-content-center">
        <div class="col-md-8">
            <div class="card">
                <div class="card-header bg-primary text-white">
                    <h4 class="mb-0">Web Page Archiver (Synchronous)</h4>
                </div>
                <div class="card-body">
                    <p class="text-muted small">
                        Enter a URL to download a self-contained HTML file. <br>
                        <strong>Note:</strong> Vercel's free tier has a strict 10-second timeout. Highly complex pages may fail to process in time.
                    </p>
                    
                    <form id="archiveForm">
                        <div class="mb-3">
                            <label for="url" class="form-label">Target URL</label>
                            <input type="url" class="form-control" id="url" placeholder="https://example.com" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100" id="btn-submit">Generate & Download Archive</button>
                    </form>

                    <div id="loading-container" class="mt-4 text-center">
                        <div class="spinner-border text-primary" role="status">
                            <span class="visually-hidden">Loading...</span>
                        </div>
                        <p class="mt-2 fw-bold" id="status-text">Downloading and converting assets... Please wait.</p>
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
            let errorMsg = "Server error or Vercel 10-second timeout exceeded.";
            try {
                const data = await response.json();
                if (data.error) errorMsg = data.error;
            } catch(e) {}
            throw new Error(errorMsg);
        }

        // Handle binary file download directly
        const blob = await response.blob();
        const downloadUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        
        // Extract domain for filename
        const urlObj = new URL(url);
        const domain = urlObj.hostname.replace(/\./g, '_');
        
        a.href = downloadUrl;
        a.download = `${domain}_archive.html`;
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

class WebArchiver:
    def __init__(self, target_url):
        self.target_url = target_url
        self.session = requests.Session()
        retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self.session.mount('http://', HTTPAdapter(max_retries=retries))
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })
        self.resource_cache = {}

    def fetch_resource(self, url, is_text=False):
        if url.startswith('data:'): return url 
        if url in self.resource_cache: return self.resource_cache[url]

        try:
            # Shortened timeout to survive Vercel's limits
            resp = self.session.get(url, stream=True, timeout=5)
            resp.raise_for_status()

            if int(resp.headers.get('Content-Length', 0)) > MAX_RESOURCE_SIZE:
                return None

            content = resp.content
            if is_text:
                encoding = resp.encoding if resp.encoding else 'utf-8'
                result = content.decode(encoding, errors='replace')
                self.resource_cache[url] = result
                return result
            else:
                mime_type = resp.headers.get('Content-Type', '').split(';')[0]
                if not mime_type:
                    mime_type = mimetypes.guess_type(url)[0] or 'application/octet-stream'
                
                b64_data = base64.b64encode(content).decode('utf-8')
                data_uri = f"data:{mime_type};base64,{b64_data}"
                self.resource_cache[url] = data_uri
                return data_uri
        except Exception:
            return None

    def process_css_content(self, css_text, base_url):
        def replacer(match):
            quote = match.group(1)
            inner_url = match.group(2).strip()
            if inner_url.startswith('data:') or inner_url.startswith('#'):
                return match.group(0)
            
            absolute_url = urljoin(base_url, inner_url)
            data_uri = self.fetch_resource(absolute_url)
            if data_uri: return f"url({quote}{data_uri}{quote})"
            return match.group(0)
        return CSS_URL_REGEX.sub(replacer, css_text)

    def process(self):
        main_resp = self.session.get(self.target_url, timeout=10)
        main_resp.raise_for_status()
        
        main_resp.encoding = main_resp.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(main_resp.text, 'html.parser')
        
        base_tag = soup.find('base')
        base_url = urljoin(self.target_url, base_tag['href']) if base_tag and base_tag.has_attr('href') else main_resp.url

        assets_to_download = []
        
        for img in soup.find_all(['img', 'source']):
            for attr in ['src', 'srcset', 'data-src']:
                if img.has_attr(attr):
                    urls = [u.split()[0] for u in img[attr].split(',')]
                    for u in urls:
                        if u and not u.startswith('data:'):
                            assets_to_download.append((img, attr, u, urljoin(base_url, u), 'image'))

        for link in soup.find_all('link', rel='stylesheet'):
            if link.has_attr('href'):
                u = link['href']
                assets_to_download.append((link, 'href', u, urljoin(base_url, u), 'css'))

        for script in soup.find_all('script', src=True):
            u = script['src']
            assets_to_download.append((script, 'src', u, urljoin(base_url, u), 'js'))

        downloaded_data = {}
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
            future_to_req = {}
            for tag, attr, orig_url, abs_url, res_type in assets_to_download:
                if abs_url not in downloaded_data:
                    future = executor.submit(self.fetch_resource, abs_url, is_text=(res_type in ['css', 'js']))
                    future_to_req[future] = (abs_url, res_type)

            for future in as_completed(future_to_req):
                abs_url, res_type = future_to_req[future]
                try:
                    data = future.result()
                    if data:
                        if res_type == 'css':
                            data = self.process_css_content(data, abs_url)
                        downloaded_data[abs_url] = data
                except Exception:
                    pass

        for tag, attr, orig_url, abs_url, res_type in assets_to_download:
            data = downloaded_data.get(abs_url)
            if not data:
                tag[attr] = abs_url
                continue
                
            if res_type == 'image':
                if attr == 'srcset':
                    del tag['srcset']
                    tag['src'] = data
                else:
                    tag[attr] = data
            elif res_type == 'css':
                style_tag = soup.new_tag('style')
                style_tag.string = data
                tag.replace_with(style_tag)
            elif res_type == 'js':
                script_tag = soup.new_tag('script')
                script_tag.string = data
                if tag.has_attr('type'): script_tag['type'] = tag['type']
                tag.replace_with(script_tag)

        for style in soup.find_all('style'):
            if style.string:
                style.string = self.process_css_content(style.string, base_url)

        if base_tag: base_tag.decompose()
        for meta in soup.find_all('meta', attrs={'http-equiv': lambda x: x and x.lower() == 'content-security-policy'}):
            meta.decompose()

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
        archiver = WebArchiver(url)
        final_html = archiver.process()
        
        # Write to Vercel's temporary directory
        job_id = str(uuid.uuid4())
        filepath = os.path.join(TEMP_DIR, f"archive_{job_id}.html")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(final_html)
            
        domain = parsed.netloc.replace('.', '_')
        return send_file(
            filepath, 
            as_attachment=True, 
            download_name=f"{domain}_archive.html", 
            mimetype='text/html'
        )
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f"Failed to connect to the target website: {str(e)}"}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)

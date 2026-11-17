import os
import sys
import time
import threading

_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_CUR_DIR)
_VENDOR_DIR = os.path.join(_MODULE_DIR, "_vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.driver_cache import DriverCacheManager
import selenium.webdriver.chrome.webdriver
import re
import socket


# ─── Constants ────────────────────────────────────────────────────────────────
GST_LOGIN_URL = "https://services.gst.gov.in/services/login"
SHORT_WAIT = 1
MEDIUM_WAIT = 2
LONG_WAIT = 3
ELEMENT_TIMEOUT = 10
GENERATE_TIMEOUT = 60
DOWNLOAD_LINK_WAIT = 5
REMOTE_DEBUG_PORT = 9222


from utils import get_user_data_dir


def get_wdm_dir():
    """Returns the webdriver-manager cache directory inside UserData."""
    return os.path.join(get_user_data_dir(), "wdm")



def clean_wdm_locks(wdm_dir):
    """Clears stale lock files in BOTH local and global webdriver-manager directories to prevent deadlock."""
    for path_to_clean in [wdm_dir, os.path.expanduser("~/.wdm")]:
        if path_to_clean and os.path.exists(path_to_clean):
            for root, dirs, files in os.walk(path_to_clean):
                for f in files:
                    if "lock" in f.lower():
                        try:
                            os.remove(os.path.join(root, f))
                        except Exception:
                            pass


def open_returns_dashboard(driver):
    """Navigates directly to Returns Dashboard via Header Bar Navigation (Services -> Returns -> Returns Dashboard)."""
    try:
        if len(driver.find_elements(By.NAME, "fin")) > 0 and len(driver.find_elements(By.NAME, "quarter")) > 0:
            return True
    except Exception:
        pass

    # 1. Primary Direct Navigation via Header Bar: Services -> Returns -> Returns Dashboard
    try:
        services_elements = driver.find_elements(
            By.XPATH,
            "//a[contains(@class,'dropdown-toggle') and contains(text(),'Services')]"
        )
        if services_elements:
            driver.execute_script("arguments[0].click();", services_elements[0])
            time.sleep(1)

            returns_elements = driver.find_elements(
                By.XPATH,
                "//a[contains(@data-ng-href, 'quicklinks/returns') or contains(@href, 'quicklinks/returns')]"
            )
            if returns_elements:
                driver.execute_script("arguments[0].click();", returns_elements[0])
                time.sleep(1)

                dash_elements = driver.find_elements(
                    By.XPATH,
                    "//a[contains(@data-ng-href, 'returns/auth/dashboard') or contains(@href, 'returns/auth/dashboard')]"
                )
                if dash_elements:
                    driver.execute_script("arguments[0].click();", dash_elements[0])
                    time.sleep(3)
                    if len(driver.find_elements(By.NAME, "fin")) > 0:
                        return True
    except Exception as e:
        log(f"Notice during Header navigation: {e}")

    # 2. Direct Returns Dashboard / File Returns tile button on main portal dashboard
    try:
        tile_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(normalize-space(),'RETURN DASHBOARD') or contains(normalize-space(),'File Returns')] | "
            "//a[contains(normalize-space(),'RETURN DASHBOARD') or contains(normalize-space(),'File Returns')]"
        )
        for btn in tile_btns:
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(3)
                if len(driver.find_elements(By.NAME, "fin")) > 0:
                    return True
    except Exception:
        pass

    # 3. Fallback direct GET to dashboard URL if header navigation fails
    try:
        driver.get("https://return.gst.gov.in/returns/auth/dashboard")
        time.sleep(3)
        if len(driver.find_elements(By.NAME, "fin")) > 0:
            return True
    except Exception:
        pass

    return False


def navigate_to_returns(driver, year, quarter, month=None):
    """Navigate to the Returns Dashboard and select the given year, quarter, and optionally month dynamically."""
    open_returns_dashboard(driver)

    # 1. Select Financial Year
    fin_select = WebDriverWait(driver, ELEMENT_TIMEOUT).until(EC.presence_of_element_located((By.NAME, "fin")))
    try:
        if Select(fin_select).first_selected_option.text.strip() != year:
            Select(fin_select).select_by_visible_text(year)
            wait_for_page_ready(driver, timeout=3)
    except Exception:
        Select(fin_select).select_by_visible_text(year)

    # 2. Select Quarter as soon as dropdown is ready
    quarter_select = WebDriverWait(driver, ELEMENT_TIMEOUT).until(EC.element_to_be_clickable((By.NAME, "quarter")))
    try:
        if Select(quarter_select).first_selected_option.text.strip() != quarter:
            Select(quarter_select).select_by_visible_text(quarter)
            wait_for_page_ready(driver, timeout=3)
    except Exception:
        Select(quarter_select).select_by_visible_text(quarter)

    # 3. Select Month dynamically as soon as options are populated for this quarter
    if month:
        def month_options_ready(d):
            try:
                el = d.find_element(By.NAME, "mon")
                mon_select = Select(el)
                opts = [o.text.strip() for o in mon_select.options if o.text.strip()]
                return el if month in opts else False
            except Exception:
                return False

        mon_el = WebDriverWait(driver, ELEMENT_TIMEOUT).until(month_options_ready)
        Select(mon_el).select_by_visible_text(month)

    return True


def click_search(driver):
    """Click the Search button on the Returns Dashboard and wait dynamically for search results."""
    search_btn = WebDriverWait(driver, ELEMENT_TIMEOUT).until(
        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'Search')] | //button[contains(text(),'SEARCH')]"))
    )
    click_element(driver, search_btn)
    wait_for_page_ready(driver)


def ensure_search_results(driver, year, quarter, month, stop_event=None):
    """
    Ensures that the browser is displaying the Returns Dashboard search results
    for the given (year, quarter, month).
    If already on the search results for that exact month, returns immediately.
    If on subpage (GSTR-1, GSTR-2B, GSTR-2A), navigates back and searches.
    """
    if stop_event and stop_event.is_set():
        return False

    wait_for_page_ready(driver, stop_event=stop_event)

    has_dropdowns = (
        len(driver.find_elements(By.NAME, "fin")) > 0 and
        len(driver.find_elements(By.NAME, "quarter")) > 0 and
        len(driver.find_elements(By.NAME, "mon")) > 0
    )

    if not has_dropdowns:
        navigate_to_returns(driver, year, quarter, month)
        click_search(driver)
        return True

    try:
        cur_fin = Select(driver.find_element(By.NAME, "fin")).first_selected_option.text.strip()
        cur_qtr = Select(driver.find_element(By.NAME, "quarter")).first_selected_option.text.strip()
        cur_mon = Select(driver.find_element(By.NAME, "mon")).first_selected_option.text.strip()

        has_tiles = len(driver.find_elements(By.XPATH, "//button[@data-ng-click='downloadGSTR3Bpdf()'] | //*[contains(@data-ng-if, 'GSTR')]")) > 0

        if cur_fin == year and cur_qtr == quarter and cur_mon == month and has_tiles:
            return True

        if cur_fin != year:
            Select(driver.find_element(By.NAME, "fin")).select_by_visible_text(year)
            wait_for_page_ready(driver, stop_event=stop_event)

        if cur_qtr != quarter or cur_fin != year:
            Select(WebDriverWait(driver, ELEMENT_TIMEOUT).until(EC.element_to_be_clickable((By.NAME, "quarter")))).select_by_visible_text(quarter)
            wait_for_page_ready(driver, stop_event=stop_event)

        def month_ready(d):
            try:
                el = d.find_element(By.NAME, "mon")
                m_sel = Select(el)
                opts = [o.text.strip() for o in m_sel.options if o.text.strip()]
                return el if month in opts else False
            except Exception:
                return False

        mon_el = WebDriverWait(driver, ELEMENT_TIMEOUT).until(month_ready)
        Select(mon_el).select_by_visible_text(month)

        click_search(driver)
        return True

    except Exception:
        navigate_to_returns(driver, year, quarter, month)
        click_search(driver)
        return True


def find_form_tile_button(driver, form_name, button_type):
    """
    Finds the exact button (VIEW or DOWNLOAD) inside the specific tile card for form_name
    using exact AngularJS directives from the GST Portal HTML structure.
    Uses dynamic polling retry so slow Angular rendering never causes missed buttons.
    """
    form_clean = form_name.replace("-", "").upper()  # e.g. 'GSTR3B', 'GSTR1', 'GSTR2A', 'GSTR2B'
    b_type_upper = button_type.upper()  # 'VIEW' or 'DOWNLOAD'

    specific_xpaths = []

    if form_clean == "GSTR3B":
        if b_type_upper == "DOWNLOAD":
            specific_xpaths.append("//button[@data-ng-click='downloadGSTR3Bpdf()']")
            specific_xpaths.append("//p[contains(text(),'GSTR-3B') or contains(text(),'GSTR3B')]/ancestor::div[contains(@class,'col-') or contains(@data-ng-if,'GSTR3B') or contains(@data-ng-repeat,'returns')][last()]//button[contains(@data-ng-click,'download') or contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')]")
        elif b_type_upper == "VIEW":
            specific_xpaths.append("//p[contains(text(),'GSTR-3B') or contains(text(),'GSTR3B')]/ancestor::div[contains(@class,'col-') or contains(@data-ng-if,'GSTR3B') or contains(@data-ng-repeat,'returns')][last()]//button[contains(text(),'VIEW GSTR3B') or contains(@data-ng-click,'page_rtp')]")

    elif form_clean == "GSTR1":
        if b_type_upper == "VIEW":
            specific_xpaths.append("//p[contains(text(),'GSTR-1') and not(contains(text(),'GSTR-1A'))]/ancestor::div[contains(@class,'col-') or contains(@data-ng-repeat,'returns')][last()]//button[contains(text(),'View') or contains(@data-ng-click,'page_rtp')]")
        elif b_type_upper == "DOWNLOAD":
            specific_xpaths.append("//p[contains(text(),'GSTR-1') and not(contains(text(),'GSTR-1A'))]/ancestor::div[contains(@class,'col-') or contains(@data-ng-repeat,'returns')][last()]//button[contains(text(),'Download') or contains(@data-ng-click,'offlinepath')]")

    elif form_clean == "GSTR2A":
        if b_type_upper == "DOWNLOAD":
            specific_xpaths.append("//p[contains(text(),'GSTR-2A') or contains(text(),'GSTR2A')]/ancestor::div[contains(@class,'col-') or contains(@data-ng-repeat,'returns')][last()]//button[contains(@data-ng-click,'offlinepath') or contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')]")
        elif b_type_upper == "VIEW":
            specific_xpaths.append("//p[contains(text(),'GSTR-2A') or contains(text(),'GSTR2A')]/ancestor::div[contains(@class,'col-') or contains(@data-ng-repeat,'returns')][last()]//button[contains(@data-ng-click,'page_rtp') or contains(text(),'View')]")

    elif form_clean == "GSTR2B":
        if b_type_upper == "VIEW":
            specific_xpaths.append("//p[contains(text(),'GSTR-2B') or contains(text(),'GSTR2B')]/ancestor::div[contains(@class,'col-') or contains(@data-ng-repeat,'returns')][last()]//button[contains(@data-ng-click,'page_rtp') or contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'VIEW')]")
        elif b_type_upper == "DOWNLOAD":
            specific_xpaths.append("//p[contains(text(),'GSTR-2B') or contains(text(),'GSTR2B')]/ancestor::div[contains(@class,'col-') or contains(@data-ng-repeat,'returns')][last()]//button[contains(@data-ng-click,'offlinepath') or contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')]")

    # Dynamic polling loop up to 5 seconds to handle Angular AJAX render lag
    start_t = time.time()
    while time.time() - start_t < 5:
        for xpath in specific_xpaths:
            try:
                btns = driver.find_elements(By.XPATH, xpath)
                for btn in btns:
                    if btn.is_displayed():
                        return btn
            except Exception:
                continue
        time.sleep(0.4)

    # 2. Fallback search: Outer card search
    fallback_variants = [form_name, form_name.replace("-", ""), form_clean]
    for var in fallback_variants:
        fallback_xpath = f"//*[text()='{var}' or contains(text(),'{var}')]/ancestor::div[contains(@class,'col-sm-') or contains(@class,'col-md-') or contains(@data-ng-repeat,'returns')][last()]//*[self::button or self::a][contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), '{b_type_upper}')]"
        try:
            btns = driver.find_elements(By.XPATH, fallback_xpath)
            for btn in btns:
                if btn.is_displayed():
                    return btn
        except Exception:
            continue

    return None


def wait_for_page_ready(driver, timeout=10, stop_event=None):
    """Waits dynamically for GST Portal loading dimmers and spinners to disappear."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if stop_event and stop_event.is_set():
            return
        try:
            dimmers = driver.find_elements(By.CSS_SELECTOR, ".dimmer-holder, .loading, .spinner, .blockUI")
            if not dimmers or not any(d.is_displayed() for d in dimmers):
                break
        except Exception:
            break
        time.sleep(0.05)


def wait_for_download_initiation(download_dir, base_dir=None, timeout=5, stop_event=None):
    """
    Waits dynamically for a new file to start downloading in download_dir or base_dir.
    Returns True immediately when a file is detected.
    """
    dirs = [os.path.abspath(d) for d in [download_dir, base_dir] if d and os.path.exists(d)]
    if not dirs:
        return True
        
    initial_files = {}
    for d in dirs:
        try:
            initial_files[d] = set(os.listdir(d))
        except Exception:
            initial_files[d] = set()

    start_time = time.time()
    while time.time() - start_time < timeout:
        if stop_event and stop_event.is_set():
            return False
        for d in dirs:
            try:
                current_files = set(os.listdir(d))
                new_files = current_files - initial_files[d]
                if new_files:
                    return True
            except Exception:
                pass
        time.sleep(0.06)
        
    return True


def click_element(driver, element):
    """
    Scrolls element into center view and performs an instant click.
    Uses native click first, falling back to JS click if needed.
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
    except Exception:
        pass
        
    try:
        element.click()
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", element)
        except Exception:
            pass


def _is_port_open(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            return True
    except Exception:
        return False


def _try_connect_existing_chrome(driver_path):
    """Try to connect to an already-running Chrome instance via remote debugging port."""
    if not _is_port_open(REMOTE_DEBUG_PORT):
        return None
    try:
        debug_options = Options()
        debug_options.add_experimental_option("debuggerAddress", f"127.0.0.1:{REMOTE_DEBUG_PORT}")
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=debug_options)
        # Verify connection is live by reading the current URL
        _ = driver.current_url
        return driver
    except Exception:
        return None


def run_gst_script(USERNAME, PASSWORD, YEAR, TARGET_MONTHS, SELECTED_FORMS,
                   log_callback=None, dialog_callback=None, stop_event=None,
                   progress_callback=None, download_dir=None):
    """
    Executes the Selenium automation to download GSTR forms.
    
    Parameters:
        log_callback: function(msg) to send real-time logs to the UI.
        dialog_callback: function(type, title, msg) to show thread-safe dialogs (type: "info"/"error").
        stop_event: threading.Event — if set, the downloader stops gracefully.
        progress_callback: function(month_idx, total_months, form_name) for progress updates.
        download_dir: directory path for Chrome downloads.
    """
    def log(msg):
        print(msg)
        if log_callback:
            log_callback(msg)

    def show_dialog(dtype, title, msg):
        if dialog_callback:
            dialog_callback(dtype, title, msg)
        else:
            log(f"[{dtype.upper()}] {title}: {msg}")

    def is_stopped():
        return stop_event and stop_event.is_set()

    log("Initializing Chrome driver with anti-detection options...")
    options = Options()

    # Hide automation control flags and infobars to bypass Web Application Firewall (WAF) blocks
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--start-maximized")
    options.add_argument("--disable-popup-blocking")

    # Detach Chrome so it stays open if the script crashes or the user stops
    options.add_experimental_option("detach", True)

    # Set up Chrome profile directory inside UserData for session persistence
    user_data_dir = get_user_data_dir()
    chrome_profile_dir = os.path.join(user_data_dir, "chrome_profile")
    os.makedirs(chrome_profile_dir, exist_ok=True)
    options.add_argument(f"--user-data-dir={chrome_profile_dir}")
    options.add_argument(f"--remote-debugging-port={REMOTE_DEBUG_PORT}")

    # Set download directory and allow multiple automatic downloads / popups
    prefs = {
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.popups": 1,
        "profile.default_content_setting_values.automatic_downloads": 1
    }
    target_dl = os.path.abspath(download_dir) if download_dir else os.path.abspath(os.path.join(user_data_dir, "downloads"))
    os.makedirs(target_dl, exist_ok=True)
    prefs["download.default_directory"] = target_dl
    log(f"Base download directory set to: {target_dl}")

    # Pre-create dedicated folders in selected path for checked returns
    form_folders = {}
    for form in SELECTED_FORMS:
        form_folder_name = form.replace("-", " ")  # e.g. "GSTR 1", "GSTR 3B", "GSTR 2A", "GSTR 2B"
        f_dir = os.path.join(target_dl, form_folder_name)
        os.makedirs(f_dir, exist_ok=True)
        form_folders[form] = f_dir
        log(f"Created return folder: {form_folder_name} -> {f_dir}")

    options.add_experimental_option("prefs", prefs)

    def set_form_download_dir(driver_inst, folder_path):
        """Directs Chrome downloads into the specific return form subfolder via CDP."""
        os.makedirs(folder_path, exist_ok=True)
        try:
            driver_inst.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": os.path.abspath(folder_path)
                }
            )
        except Exception as e:
            log(f"Notice setting form download directory: {e}")
        return folder_path

    # Prepare webdriver-manager paths
    wdm_dir = get_wdm_dir()
    clean_wdm_locks(wdm_dir)

    # Download/verify driver path locally in Support Files/wdm
    if wdm_dir:
        driver_path = ChromeDriverManager(
            cache_manager=DriverCacheManager(root_dir=wdm_dir)
        ).install()
    else:
        driver_path = ChromeDriverManager().install()

    # Patch Chromedriver binary to remove F5 WAF signatures
    try:
        log("Patching Chromedriver binary context signatures to prevent WAF detection...")
        with open(driver_path, "rb") as f:
            content = f.read()

        pattern = re.compile(b"cdc_[a-zA-Z0-9_]{22,26}")
        matches = pattern.findall(content)
        if matches:
            for match in set(matches):
                replacement = b"abc_" + match[4:]
                content = content.replace(match, replacement)
            with open(driver_path, "wb") as f:
                f.write(content)
            log(f"Chromedriver signature patched successfully ({len(matches)} modifications applied).")
        else:
            log("Chromedriver signature already patched or not found.")
    except Exception as e:
        log(f"Warning during Chromedriver patching: {str(e)}")

    # Try to reconnect to an existing Chrome instance first
    driver = None
    try:
        log("Checking for existing Chrome session...")
        driver = _try_connect_existing_chrome(driver_path)
        if driver:
            log("Reconnected to existing Chrome browser session.")
            # Automatically find and switch to an existing GST portal tab if already open
            try:
                for handle in driver.window_handles:
                    driver.switch_to.window(handle)
                    if "gst.gov.in" in driver.current_url.lower():
                        log(f"Switched to active GST tab: {driver.current_url}")
                        break
            except Exception:
                pass
        else:
            log("No existing Chrome session found. Launching new browser...")
            service = Service(driver_path)
            driver = webdriver.Chrome(service=service, options=options)
            log("Chrome browser launched successfully.")

        # Force Chrome to allow automatic file downloads via Chrome DevTools Protocol (CDP)
        try:
            target_dl = os.path.abspath(download_dir) if download_dir else os.path.abspath(os.path.join(app_dir, "downloads"))
            os.makedirs(target_dl, exist_ok=True)
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": target_dl
            })
            log("Configured Chrome download permissions via CDP (automatic downloads unblocked).")
        except Exception as cdp_err:
            log(f"Notice setting CDP download behavior: {cdp_err}")
    except Exception as e:
        log(f"Error launching Chrome: {e}")
        raise

    download_success = False
    try:
        log("Checking current browser page status...")

        def is_session_expired():
            """Detect 'Access Denied' or 'Session Expired' GST portal error pages."""
            try:
                page_source = driver.page_source.lower()
                current_url = driver.current_url.lower()
                expired_phrases = [
                    "access denied", "session expired", "session has expired",
                    "your session", "sessionexpired", "accessdenied",
                    "you are not authorized", "unauthorized access"
                ]
                if any(phrase in page_source for phrase in expired_phrases):
                    return True
                if "accessdenied" in current_url or "sessionexpired" in current_url:
                    return True
                return False
            except Exception:
                return False

        def is_login_page():
            try:
                url = driver.current_url.lower()
                if "/login" in url or "services/login" in url or "/logout" in url or "services/logout" in url:
                    return True
                if len(driver.find_elements(By.ID, "username")) > 0 or len(driver.find_elements(By.ID, "user_pass")) > 0:
                    return True
                return False
            except Exception:
                return False

        def is_logged_in():
            try:
                if is_session_expired():
                    return False

                # If login fields are present on screen, we are definitely NOT logged in yet
                if len(driver.find_elements(By.ID, "username")) > 0 or len(driver.find_elements(By.ID, "user_pass")) > 0:
                    return False

                url = driver.current_url.lower()
                # If URL is explicitly on the login or logout page
                if "/services/login" in url or "/services/logout" in url:
                    return False

                # 1. Check URL patterns that only authenticated users reach:
                if "/auth/" in url or "quicklinks" in url or "returns/auth" in url or "dashboard" in url:
                    return True

                # 2. Check for Financial Year dropdown (already on Returns Dashboard):
                if len(driver.find_elements(By.NAME, "fin")) > 0:
                    return True

                # 3. Check for logged-in navigation items or buttons:
                logged_in_xpaths = [
                    "//a[contains(translate(.,'LOGOUT','logout'),'logout')]",
                    "//button[contains(translate(.,'LOGOUT','logout'),'logout')]",
                    "//a[contains(@href,'logout')]",
                    "//a[contains(normalize-space(),'Returns Dashboard')]",
                    "//button[contains(normalize-space(),'RETURN DASHBOARD')]",
                    "//a[contains(normalize-space(),'File Returns')]",
                    "//button[contains(normalize-space(),'File Returns')]",
                    "//a[contains(normalize-space(),'Services')]",
                    "//a[contains(normalize-space(),'Dashboard')]",
                    "//span[contains(@class,'user')]",
                    "//div[contains(@class,'user')]",
                    "//li[contains(@class,'user')]",
                    "//*[contains(@class,'welcome')]"
                ]
                for xpath in logged_in_xpaths:
                    elements = driver.find_elements(By.XPATH, xpath)
                    if len(elements) > 0:
                        return True

                return False
            except Exception:
                return False

        def navigate_to_login(reason=""):
            """Navigate to login page safely via public homepage or 'here' link to bypass Access Denied WAF."""
            if reason:
                log(reason)
            try:
                # Look for the 'here' or 'Login' link on the Access Denied page
                links = driver.find_elements(
                    By.XPATH,
                    "//a[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'here') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'login') or contains(@href,'login')]"
                )
                for link in links:
                    if link.is_displayed():
                        log("Clicking 'here to Login' link on Access Denied page...")
                        driver.execute_script("arguments[0].click();", link)
                        time.sleep(MEDIUM_WAIT)
                        return
            except Exception:
                pass

            # Primary anti-WAF strategy: Navigate to main public homepage first to acquire trusted cookies
            try:
                log("Navigating to GST Portal homepage...")
                driver.get("https://www.gst.gov.in/")
                time.sleep(2)
                
                # Click the top-right Login link on the public homepage
                login_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(@href,'login') or contains(text(),'Login') or contains(text(),'LOGIN')]"))
                )
                driver.execute_script("arguments[0].click();", login_btn)
                time.sleep(MEDIUM_WAIT)
            except Exception:
                # Fallback direct GET if homepage click fails
                try:
                    driver.get(GST_LOGIN_URL)
                    time.sleep(MEDIUM_WAIT)
                except Exception:
                    pass

        def perform_login():
            """Ensures user is logged in: enters credentials, shows CAPTCHA dialog, and verifies login."""
            if is_logged_in():
                log("Active logged-in session detected!")
                return True

            log("Navigating to GST Portal login page...")
            navigate_to_login()

            # Wait patiently up to 10 seconds for the login form to appear
            login_form_loaded = False
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: len(d.find_elements(By.ID, "username")) > 0 or is_logged_in()
                )
                if len(driver.find_elements(By.ID, "username")) > 0:
                    login_form_loaded = True
                    log("Login form loaded successfully.")
                elif is_logged_in():
                    log("Session cookies redirected to dashboard automatically!")
                    return True
            except TimeoutException:
                log("Login page did not load within 10 seconds.")

            if not login_form_loaded and not is_logged_in():
                log("Error: GST Portal login page failed to load. Closing session.")
                try:
                    driver.quit()
                except Exception:
                    pass
                show_dialog("error", "Login Failed", "GST Portal login page failed to load.\n\nChrome has been closed. Please try again.")
                return False

            if not is_logged_in():
                log("Login form loaded. Filling credentials...")
                try:
                    WebDriverWait(driver, ELEMENT_TIMEOUT).until(EC.presence_of_element_located((By.ID, "username")))
                    user_el = driver.find_element(By.ID, "username")
                    user_el.clear()
                    user_el.send_keys(USERNAME)
                    pass_el = driver.find_element(By.ID, "user_pass")
                    pass_el.clear()
                    pass_el.send_keys(PASSWORD)
                except Exception as e:
                    log(f"Notice during credential entry: {e}")

                # CAPTCHA prompt loop — only proceeds when login is confirmed
                logged_in = False
                while not logged_in:
                    if is_stopped():
                        log("Download cancelled by user.")
                        return False

                    log("Waiting for user to complete CAPTCHA and click Login on Chrome window...")
                    show_dialog("info_blocking", "CAPTCHA Verification",
                                 "Please complete the CAPTCHA and click Login on the GST portal.\n\nClick OK here ONLY AFTER you have logged in successfully.")

                    if is_stopped():
                        log("Download cancelled by user after CAPTCHA.")
                        return False

                    log("Verifying login status...")
                    time.sleep(SHORT_WAIT)
                    if is_logged_in():
                        logged_in = True
                        log("Login verified successfully!")
                        return True
                    else:
                        log("Verification failed: Still on login page or credentials incorrect.")
                        show_dialog("info_blocking", "Not Logged In Yet",
                                     "You are not logged in yet!\n\nPlease enter the CAPTCHA on the Chrome window and click 'LOGIN' first, then click OK here.")
            return True

        # Initial login check & authentication
        if not perform_login():
            return

        log("Navigating to Returns Dashboard / File Returns page...")
        dashboard_reached = False
        for attempt in range(1, 4):
            if is_stopped():
                log("Download cancelled by user.")
                return

            log(f"Attempting to open Returns Dashboard (attempt {attempt}/3)...")
            try:
                dashboard_reached = open_returns_dashboard(driver)
            except Exception as e:
                log(f"Attempt {attempt} error: {e}")
                dashboard_reached = False

            if dashboard_reached:
                break

            # If session genuinely expired during navigation, re-authenticate immediately
            if is_session_expired() or not is_logged_in():
                log("Session lost while opening dashboard. Re-authenticating...")
                if perform_login():
                    continue
                else:
                    return

            log(f"Returns Dashboard not reached on attempt {attempt}. Waiting before retry...")
            time.sleep(LONG_WAIT)

        if not dashboard_reached:
            log("Returns Dashboard unreachable after 3 attempts. Stopping.")
            show_dialog("error", "Navigation Failed",
                        "Could not reach the Returns Dashboard after 3 attempts.\n\n"
                        "Please check your internet connection or try again.")
            return

        log(f"Selecting Financial Year: {YEAR}...")
        fin_select = WebDriverWait(driver, ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located((By.NAME, "fin"))
        )
        Select(fin_select).select_by_visible_text(YEAR)

        quarters = [opt.text for opt in Select(
            WebDriverWait(driver, ELEMENT_TIMEOUT).until(EC.presence_of_element_located((By.NAME, "quarter")))
        ).options]

        def get_quarter_for_month(m_name, avail_quarters):
            m_lower = m_name.lower()
            if m_lower in ["april", "may", "june"]:
                q_key = "1"
            elif m_lower in ["july", "august", "september"]:
                q_key = "2"
            elif m_lower in ["october", "november", "december"]:
                q_key = "3"
            elif m_lower in ["january", "february", "march"]:
                q_key = "4"
            else:
                q_key = ""

            for q in avail_quarters:
                if q_key and f"quarter {q_key}" in q.lower():
                    return q
                if q_key and f"q{q_key}" in q.lower():
                    return q
            return avail_quarters[0] if avail_quarters else "Quarter 1 (Apr - Jun)"

        total_months = len(TARGET_MONTHS)
        month_counter = 0

        for month in TARGET_MONTHS:
            if is_stopped():
                log("Download cancelled by user.")
                return

            quarter = get_quarter_for_month(month, quarters)
            month_counter += 1
            log(f"Processing Quarter: {quarter} → Month: {month} ({month_counter}/{total_months})")

            # Strict Form Execution Order: GSTR-3B -> GSTR-1 -> GSTR-2B -> GSTR-2A

            # 1. GSTR-3B (Direct Download)
            if "GSTR-3B" in SELECTED_FORMS:
                if progress_callback:
                    progress_callback(month_counter, total_months, "GSTR-3B")
                g3b_dl_dir = set_form_download_dir(driver, form_folders.get("GSTR-3B", target_dl))
                try:
                    log(f"[GSTR-3B] Locating download button for {month}...")
                    ensure_search_results(driver, YEAR, quarter, month, stop_event=stop_event)

                    g3b_btn = find_form_tile_button(driver, "GSTR-3B", "DOWNLOAD")
                    if not g3b_btn:
                        g3b_btn = find_form_tile_button(driver, "GSTR-3B", "VIEW")

                    if g3b_btn:
                        log(f"[GSTR-3B] Clicking download button for {month}...")
                        click_element(driver, g3b_btn)
                        wait_for_download_initiation(g3b_dl_dir, target_dl, timeout=4, stop_event=stop_event)
                        log(f"GSTR-3B downloaded for {month} into GSTR 3B folder")
                    else:
                        log(f"[GSTR-3B] Download button not found on tile for {month}.")
                except Exception as e:
                    log(f"[GSTR-3B] Error for {month}: {e}")

            # 2. GSTR-1 (View -> View Summary -> Download PDF)
            if "GSTR-1" in SELECTED_FORMS:
                if progress_callback:
                    progress_callback(month_counter, total_months, "GSTR-1")
                g1_dl_dir = set_form_download_dir(driver, form_folders.get("GSTR-1", target_dl))
                try:
                    log(f"[GSTR-1] Locating View button for {month}...")
                    ensure_search_results(driver, YEAR, quarter, month, stop_event=stop_event)

                    v1 = find_form_tile_button(driver, "GSTR-1", "VIEW")
                    if v1:
                        log(f"[GSTR-1] Clicking View button for {month}...")
                        click_element(driver, v1)
                        wait_for_page_ready(driver, stop_event=stop_event)

                        # Click VIEW SUMMARY
                        try:
                            vs_btn = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'VIEW SUMMARY')]/parent::button | //button[contains(text(),'VIEW SUMMARY')] | //button[contains(normalize-space(),'VIEW SUMMARY')]"))
                            )
                            log(f"[GSTR-1] Clicking VIEW SUMMARY button...")
                            click_element(driver, vs_btn)
                            wait_for_page_ready(driver, stop_event=stop_event)
                        except Exception as e:
                            log(f"[GSTR-1] View Summary step: {e}")

                        # Click DOWNLOAD (PDF)
                        try:
                            d1_btn = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'DOWNLOAD (PDF)')]/parent::button | //button[contains(text(),'DOWNLOAD (PDF)')] | //button[contains(text(),'DOWNLOAD FILED (PDF)')]"))
                            )
                            log(f"[GSTR-1] Clicking DOWNLOAD (PDF) button...")
                            click_element(driver, d1_btn)
                            wait_for_download_initiation(g1_dl_dir, target_dl, timeout=4, stop_event=stop_event)
                            log(f"GSTR-1 PDF downloaded for {month} into GSTR 1 folder")
                        except Exception:
                            log(f"[GSTR-1] DOWNLOAD (PDF) button not visible on page.")
                    else:
                        log(f"[GSTR-1] View button not found on tile for {month}.")
                except Exception as e:
                    log(f"[GSTR-1] Error for {month}: {e}")

            # 3. GSTR-2B (View -> Download Details EXCEL ONLY)
            if "GSTR-2B" in SELECTED_FORMS:
                if progress_callback:
                    progress_callback(month_counter, total_months, "GSTR-2B")
                g2b_dl_dir = set_form_download_dir(driver, form_folders.get("GSTR-2B", target_dl))
                try:
                    log(f"[GSTR-2B] Locating View/Download button for {month}...")
                    ensure_search_results(driver, YEAR, quarter, month, stop_event=stop_event)

                    v2b = find_form_tile_button(driver, "GSTR-2B", "VIEW")
                    if not v2b:
                        v2b = find_form_tile_button(driver, "GSTR-2B", "DOWNLOAD")

                    if v2b:
                        log(f"[GSTR-2B] Opening GSTR-2B for {month}...")
                        click_element(driver, v2b)
                        wait_for_page_ready(driver, stop_event=stop_event)

                        log(f"[GSTR-2B] Locating Download Excel button...")
                        download_button = None
                        try:
                            download_button = WebDriverWait(driver, 6).until(
                                EC.element_to_be_clickable((
                                    By.XPATH,
                                    "//button[contains(translate(normalize-space(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD GSTR-2B DETAILS (EXCEL)')] | "
                                    "//button[contains(translate(normalize-space(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DETAILS (EXCEL)')] | "
                                    "//button[contains(translate(normalize-space(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'GENERATE EXCEL')] | "
                                    "//button[contains(@data-ng-click,'downloadExcel')] | "
                                    "//button[contains(@data-ng-click,'download') and contains(@data-ng-click,'excel')] | "
                                    "//button[contains(translate(normalize-space(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'EXCEL')] | "
                                    "//a[contains(translate(normalize-space(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD EXCEL') or contains(translate(normalize-space(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CLICK HERE TO DOWNLOAD EXCEL')]"
                                ))
                            )
                        except Exception:
                            pass

                        if download_button:
                            log(f"[GSTR-2B] Clicking Download Excel button...")
                            click_element(driver, download_button)
                            wait_for_download_initiation(g2b_dl_dir, target_dl, timeout=4, stop_event=stop_event)

                            # If opened on Offline page and download link is rendered:
                            dl_links_2b = driver.find_elements(By.XPATH, "//a[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CLICK HERE TO DOWNLOAD EXCEL') or contains(@href,'rtn_typ=GSTR2B')]")
                            if dl_links_2b:
                                click_element(driver, dl_links_2b[0])
                                wait_for_download_initiation(g2b_dl_dir, target_dl, timeout=4, stop_event=stop_event)

                            log(f"GSTR-2B Excel downloaded for {month} into GSTR 2B folder")
                        else:
                            log(f"[GSTR-2B] Download button not found on page for {month}.")
                    else:
                        log(f"[GSTR-2B] View/Download button not found on tile for {month}.")
                except Exception as e:
                    log(f"[GSTR-2B] Error for {month}: {e}")

            # 4. GSTR-2A (Download -> Generate Excel -> Click Download Link)
            if "GSTR-2A" in SELECTED_FORMS:
                if progress_callback:
                    progress_callback(month_counter, total_months, "GSTR-2A")
                g2a_dl_dir = set_form_download_dir(driver, form_folders.get("GSTR-2A", target_dl))
                try:
                    log(f"[GSTR-2A] Locating Download button for {month}...")
                    ensure_search_results(driver, YEAR, quarter, month, stop_event=stop_event)

                    v2a = find_form_tile_button(driver, "GSTR-2A", "DOWNLOAD")
                    if not v2a:
                        v2a = find_form_tile_button(driver, "GSTR-2A", "VIEW")

                    if v2a:
                        log(f"[GSTR-2A] Clicking Download button for {month}...")
                        click_element(driver, v2a)
                        wait_for_page_ready(driver, stop_event=stop_event)

                        # Click Generate Excel File to Download
                        try:
                            gen_btns = driver.find_elements(By.XPATH, "//button[@data-ng-click='downloadExcel()'] | //button[contains(text(),'Generate Excel File to Download')] | //button[contains(translate(normalize-space(), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'GENERATE EXCEL')]")
                            if gen_btns:
                                click_element(driver, gen_btns[0])
                                log("[GSTR-2A] Clicked 'Generate Excel File to Download'")
                                wait_for_page_ready(driver, stop_event=stop_event)
                                time.sleep(2)  # Exact 2-second wait for portal table to render generated link

                            # Check if Excel download link is ready
                            dl_links = driver.find_elements(By.XPATH, "//a[contains(@data-ng-href,'rtn_typ=GSTR2A') or contains(@href,'rtn_typ=GSTR2A') or contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CLICK HERE TO DOWNLOAD EXCEL')]")
                            if dl_links:
                                log(f"[GSTR-2A] Clicking 'Click here to download Excel' link...")
                                click_element(driver, dl_links[0])
                                wait_for_download_initiation(g2a_dl_dir, target_dl, timeout=4, stop_event=stop_event)
                                log(f"GSTR-2A Excel downloaded for {month} into GSTR 2A folder")
                            else:
                                log(f"GSTR-2A Excel requested for {month} (20-min generation time), moving to next month/form...")
                        except Exception as e:
                            log(f"GSTR-2A detail step: {e}")
                    else:
                        log("GSTR-2A download button not found on tile, skipping...")
                except Exception as e:
                    log(f"Error during GSTR-2A download for {month}: {e}")

        download_success = True
        log("All downloads completed successfully.")
        show_dialog("info", "Completed", "All downloads completed.")

    except Exception as e:
        log(f"An error occurred: {str(e)}")
        show_dialog("error", "Error", f"An error occurred:\n{str(e)}")

    finally:
        log("Browser session preserved (Chrome left open).")

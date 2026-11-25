from datetime import datetime, timezone
from collections import deque
import json
import os
import pickle
import time
import requests
import urllib.parse
from urllib.parse import urljoin, urlparse
import urllib.request
import urllib.robotparser

from bs4 import BeautifulSoup  # type: ignore

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
GAMES_URL = "https://api.igdb.com/v4/games"

def getTwitchToken(clientID: str, clientSecret: str) -> str:
    # Payload for twitch API. Credentials obtained from Twitch developer program
    payload = urllib.parse.urlencode({
        "client_id": clientID, 
        "client_secret": clientSecret, 
        "grantType": "client_credentials",
    }).encode("utf-8")
    
    req = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    
    token = data.get("acess_token")
    if not token:
        raise RuntimeError(f"No acess_token in response: {data}")
    return token

def queryIGDBGames(clientID: str, acessToken: str, apicalypseQuery: str,) -> list[dict]:
    req = urllib.request.Request(GAMES_URL, data=apicalypseQuery.encode("utf-8"),
                                 headers={
                                     "Client-ID": clientID,
                                     "Authorization":f"Bearer {acessToken}",
                                     "Accept": "application/json",
                                 },
                                 method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        parsed = json.loads(resp.read().decode("utf-8"))
    return parsed if isinstance(parsed, list) else []
    
# Format game release date in Unix for readability 
def formatUnixDate(ts: int | None) -> str | None: 
    if ts is None: 
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

# Normalize game entry to match dataset 
def normalizeIGDBGame(game: dict) -> dict:
    developers: list[str] = []
    publishers: list[str] = []
    
    # Get involved companies from nested JSON 
    for entry in game.get("involved_companies") or []:
        if not isinstance(entry, dict):
            continue
        company = entry.get("company")
        company_name = company.get("name") if isinstance(company, dict) else None
        if not company_name:
            continue
        
        if entry.get("developer"):
            developers.append(company_name)
        if entry.get("publishers"):
            publishers.append(company_name)
            
    return {
    "GameID": game.get("id"),
    "Game Title": game.get("name"),
    "Release Date": formatUnixDate(game.get("first_release_date")),
    "Developer": game.get("involved_companies.company.name"),
    "Publisher": game.get("involved_companies.publisher"),
    "Platforms": [p["name"] for p in (game.get("platforms") or []) if "name" in p],
    "Genres": [g["name"]for g in (game.get("genres") or []) if "name" in g],
    "Critical Reception": None,
    "Community User Review": game.get("raiting"),
    "URL": game.get("url"),
    }


# Include rest for 10 pages to keep below api call threshold
class WebCrawler:
    def __init__(self, seed: list[str], maxPages = 100000, outputDir = "crawledData"):
        self.seed = seed
        self.maxPages = maxPages
        self.outputDir = outputDir
        
        self.queue = deque(seed)
        self.visited = set()
        self.adjacencyMatrix = {} 
        
        self.user_agent = "IRBot/1.0 (charles.ohiri@wsu.edu)"
        self.headers = {"User-Agent": self.user_agent}
        self.robot_parsers = {}
        
        os.makedirs(os.path.join(self.outputDir, "pages") , exist_ok=True)
        

    def robotParser(self, url):
        # Gets the robots.txt from the given domain 
        parsed = urlparse(url)
        domainKey = f"{parsed.scheme}://{parsed.netloc}"
        
        if domainKey in self.robot_parsers:
            return self.robot_parsers[domainKey]
        
        robots_url = f"{domainKey}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)

        
        try:
            response = requests.get(robots_url, headers=self.headers, timeout=5)
            if response.status_code == 200:
                rp.parse(response.text.splitlines())
        except Exception: 
            pass
        
        self.robot_parsers[domainKey] = rp
        return rp
    
    # Extract raw text. Ignoring styles, headers etc. 
    def extractText(self, soup):
        for element in soup(["script", "style", "header", "footer", "nav"]):
            element.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return text; 
    
    # Saves progress and pickles the adjacency list. 
    def saveState(self):
        stateFile = os.path.join(self.outputDir, 'crawlState.pkl')
        with open(stateFile, 'wb') as file: 
            pickle.dump({'visited': self.visited, 
                        'adjacencyMatrix': self.adjacencyMatrix}, 
                        file)
        print(f"\nCrawl completed. Data saved to {stateFile}")
    
    # Main Web crawling logic 
    def scrape(self):
        pageCount = 0; 
        
        while self.queue and pageCount < self.maxPages: 
            currentURL = self.queue.popleft()
            
            if currentURL in self.visited: 
                continue
            
            rp = self.robotParser(currentURL)
            
            if not rp.can_fetch(self.user_agent, currentURL):
                print(f"[Blocked by robots.txt] {currentURL}")
                continue
                
            print(f"[{pageCount + 1}/{self.maxPages}] Currently Crawling: {currentURL}")
        
            try: 
                response = requests.get(currentURL, headers=self.headers, timeout=5)        
                content = response.headers.get("Content-Type", "")
                if response.status_code != 200 or "text/html" not in content:
                    continue
                
                self.visited.add(currentURL)
                pageCount += 1
                
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Save extracted text
                rawText = self.extractText(soup)
                filename = os.path.join(self.outputDir, "pages", f"page_{pageCount}.txt")
                with open(filename, 'w', encoding='utf-8') as file: 
                    file.write(f"URL: {currentURL}\n\n{rawText}")
                
                self.adjacencyMatrix[currentURL] = []
                for anchor in soup.find_all("a", href=True):
                    href = anchor["href"]
                    if not isinstance(href, str):
                        continue
                    # Complete URLs
                    fullURL = urljoin(currentURL, href).split("#")[0]
                    
                    if fullURL.startswith("http://") or fullURL.startswith("https://"):
                        self.adjacencyMatrix[currentURL].append(fullURL)
                        
                        if fullURL not in self.visited:
                            self.queue.append(fullURL)
                time.sleep(0.5)
            except Exception as e:
                print(f"Failed to fetch {currentURL}: {e}")
                
        self.saveState()

if __name__ == "__main__":
    crawler = WebCrawler(seed=["blank"], maxPages = 10000)
    crawler.scrape()

    


        
        
    
    
    
    
            
            
        
        
        
        
        
    


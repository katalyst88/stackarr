const puppeteer = require('C:/Users/jwden/ECTWatch/node_modules/puppeteer-core');
const COOKIE = process.argv[2];
const ASIN = process.argv[3] || 'B0D38LGBFZ';
const PAGES = [
  ['suggestions', '/suggestions'],
  ['discover', '/discover'],
  ['book', '/book/' + ASIN],
  ['lane', '/lane/series'],
  ['browse-author', '/browse?author=Brandon%20Sanderson'],
  ['requests', '/requests'],
  ['insights', '/insights'],
  ['settings', '/settings'],
  ['settings-connections', '/settings'],
];
(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new', args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  for (const [name, path] of PAGES) {
    for (const [w, h, tag] of [[1512, 950, 'desktop'], [390, 850, 'mobile']]) {
      const p = await browser.newPage();
      await p.setViewport({ width: w, height: h, deviceScaleFactor: 2 });
      await p.setCookie({ name: 'session', value: COOKIE, domain: 'localhost', path: '/' });
      try {
        await p.goto('http://localhost:8485' + path, { waitUntil: 'networkidle2', timeout: 50000 });
      } catch (e) { console.log('nav warn', name, tag, e.message); }
      await new Promise(r => setTimeout(r, 2000));
      if (name === 'settings-connections') {
        await p.evaluate(() => { try { Stackarr.settingsCat('connections', document.querySelectorAll('.settings-nav button')[3]); } catch (e) {} });
        await new Promise(r => setTimeout(r, 500));
      }
      if (name === 'suggestions' && tag === 'desktop') {
        // hover a card to capture the overlay
        const card = await p.$('.media-card');
        if (card) { await card.hover(); await new Promise(r => setTimeout(r, 400)); }
      }
      await p.screenshot({ path: `C:/Users/jwden/stackarr/audit/${name}-${tag}.png`, fullPage: false });
      console.log('shot', name, tag);
      await p.close();
    }
  }
  await browser.close();
})();

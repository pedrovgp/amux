import {test, expect} from './fixtures';
import {mkdtemp, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {cleanup} from './teardown';

// Ethan, 2026-10-26: a link copied from an open file is a deep link that opens
// in the browser or the amux app, and so is a worker's. The copied link is a
// real path (/f/<file>, /w/<worker>, api/deep_links.rs) because iOS matches
// paths for universal links and the sign-in door keeps a path through sign-in;
// a #fragment survives neither.
test('a copied file or worker link opens that file or worker', async ({page, context}) => {
  const dir = await mkdtemp(join(tmpdir(), 'amux deep-link-'));
  const file = join(dir, 'deck v2#1.md');
  await writeFile(file, '# The deck\n');
  try {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']).catch(() => {});
    await page.goto('/#path=' + file.replace(/%/g, '%25').replace(/#/g, '%23').replace(/\?/g, '%3F').replace(/ /g, '%20'));
    await expect.poll(() => page.evaluate('_fileData && _fileData.path'), {timeout: 15000}).toBe(file);

    // The Link button copies /f/<path>, on this server's address.
    const link = await page.evaluate(p => (window as any)._fileLinkUrl(p), file);
    const origin = new URL(page.url()).origin;
    expect(link).toBe(origin + '/f' + file.split('/').map(encodeURIComponent).join('/'));
    // The header's Link button on a wide screen; on a phone it is behind the
    // file's menu, so call the same handler the button runs.
    const btn = page.locator('#file-tab-link');
    if (await btn.isVisible()) await btn.click();
    else await page.evaluate(p => (window as any)._copyFileDeeplink(p), file);
    await expect(page.locator('#toast')).toContainText('Link copied');
    const copied = await page.evaluate(() => navigator.clipboard.readText()).catch(() => '');
    if (copied) expect(copied).toBe(link);

    // Opening it lands on the file, and the via=link marker does not stay in the address.
    await page.goto('about:blank');
    await page.goto(link);
    await expect.poll(() => page.evaluate('_fileData && _fileData.path'), {timeout: 15000}).toBe(file);
    expect(new URL(page.url()).search).toBe('');

    // A worker link lands on that worker's peek.
    await page.goto('about:blank');
    await page.goto(origin + '/w/no-such-worker-e2e');
    await expect.poll(() => page.evaluate(() => location.hash)).toContain('#peek=no-such-worker-e2e');
  } finally {
    await cleanup('remove the fixture folder', () => rm(dir, {recursive: true, force: true}));
  }
});

import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {DomainError} from './errors.mjs';
import {SQLStore} from './store.mjs';
import {HTTPGateway} from './http.mjs';
import {BrokerWorker} from './messaging.mjs';
import {utcnow} from './time.mjs';

export function settingsFromEnvironment() {
  return {
    databaseUrl: process.env.DATABASE_URL ?? '',
    rabbitmqUrl: process.env.RABBITMQ_URL ?? 'amqp://micro:micro@localhost:5672/',
    timerInterval: Number(process.env.TIMER_INTERVAL ?? 1) * 1000,
  };
}

async function readBody(request) {
  const chunks = []; let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1_048_576) throw new DomainError(413, 'Тело запроса превышает 1 MiB');
    chunks.push(chunk);
  }
  if (!size) return undefined;
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new DomainError(422, 'Некорректный JSON'); }
}

const swaggerHTML = name => `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Billiards · ${name}</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',deepLinking:true});</script></body></html>`;

export async function createApplication(name, Service, makeRoutes, {context = null, background = true} = {}) {
  const ownsContext = context === null;
  const settings = context?.settings ?? settingsFromEnvironment();
  if (ownsContext && !settings.databaseUrl) throw new Error('DATABASE_URL is required for a Node.js service');
  const ctx = context ?? {name, store: await new SQLStore(name, settings.databaseUrl).initialize(), http: new HTTPGateway(), now: utcnow, settings};
  ctx.settings ??= settings;
  ctx.now ??= utcnow;
  const service = new Service(ctx);
  const routes = makeRoutes(service);
  const spec = JSON.parse(await readFile(new URL(`../docs/openapi/${name}.json`, import.meta.url), 'utf8'));
  // A relative server URL works both through Compose ports and a reverse proxy.
  spec.servers = [{url: '/'}];
  const worker = background ? new BrokerWorker(ctx, service).start() : null;
  const timersAbort = new AbortController();
  const timers = background ? (async () => {
    while (!timersAbort.signal.aborted) {
      await delay(settings.timerInterval, undefined, {signal: timersAbort.signal}).catch(() => {});
      if (timersAbort.signal.aborted) break;
      try { await service.tick?.(); } catch (error) { console.error(`Timer failed in ${name}: ${error.message}`); }
    }
  })() : Promise.resolve();
  const server = createServer(async (request, response) => {
    const respond = (status, value, contentType = 'application/json; charset=utf-8') => {
      response.writeHead(status, {'content-type': contentType});
      response.end(contentType.startsWith('application/json') ? JSON.stringify(value) : value);
    };
    try {
      const url = new URL(request.url, 'http://localhost');
      if (request.method === 'GET' && url.pathname === '/health') return respond(200, {service: name, status: 'ok', broker_connected: Boolean(worker?.connected)});
      if (request.method === 'GET' && url.pathname === '/openapi.json') return respond(200, spec);
      if (request.method === 'GET' && url.pathname === '/docs') return respond(200, swaggerHTML(name), 'text/html; charset=utf-8');
      // Like FastAPI's default router, accept a trailing slash via redirect.
      if (url.pathname.length > 1 && url.pathname.endsWith('/')) {
        response.writeHead(307, {location: url.pathname.slice(0, -1) + url.search}); response.end(); return;
      }
      let pathExists = false;
      for (const route of routes) {
        const match = route.path.exec(url.pathname);
        if (!match) continue;
        pathExists = true;
        if (route.method !== request.method) continue;
        const value = await route.handle({params: match.slice(1).map(decodeURIComponent), query: url.searchParams, body: await readBody(request)});
        return respond(route.status ?? 200, value);
      }
      return respond(pathExists ? 405 : 404, {detail: pathExists ? 'Method Not Allowed' : 'Not Found'});
    } catch (error) {
      if (error instanceof DomainError) return respond(error.status, {detail: error.detail});
      console.error(`HTTP failure in ${name}: ${error.stack ?? error.message}`);
      return respond(500, {detail: 'Internal Server Error'});
    }
  });
  return {server, service, ctx, worker,
    listen(port = 0, host = '127.0.0.1') {
      return new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, host, () => { server.off('error', reject); resolve(server.address()); }); });
    },
    async close() {
      timersAbort.abort();
      if (server.listening) await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
      await worker?.close();
      await timers;
      if (ownsContext) { await ctx.http.close(); await ctx.store.close(); }
    },
  };
}

export function isMain(url) { return process.argv[1] === fileURLToPath(url); }
export async function serve(application) {
  const address = await application.listen(Number(process.env.PORT ?? 8000), process.env.HOST ?? '0.0.0.0');
  console.log(`${application.ctx.name} listening on ${address.address}:${address.port}`);
  let closing = false;
  const stop = async () => {
    if (closing) return;
    closing = true;
    try { await application.close(); } catch (error) { console.error(error.message); process.exitCode = 1; }
  };
  process.once('SIGTERM', stop); process.once('SIGINT', stop);
}

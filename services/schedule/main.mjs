import {createApplication, isMain, serve} from '../../common-node/runtime.mjs';
import {Service} from './service.mjs';
import {makeRoutes} from './api.mjs';

export function buildApp(context = null, background = true) {
  return createApplication('schedule', Service, makeRoutes, {context, background});
}
if (isMain(import.meta.url)) await serve(await buildApp());

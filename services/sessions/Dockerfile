FROM docker.io/library/node:24-alpine
WORKDIR /app
COPY common-node/package.json common-node/package-lock.json ./common-node/
RUN npm ci --prefix common-node --omit=dev --ignore-scripts --no-audit --no-fund
COPY common-node ./common-node
COPY services/sessions ./services/sessions
COPY docs/openapi/sessions.json ./docs/openapi/sessions.json
USER node
EXPOSE 8000
CMD ["node", "services/sessions/main.mjs"]

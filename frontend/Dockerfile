FROM node:22-slim

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app
# Created up front so the anonymous volumes in docker-compose.yml inherit node's
# ownership. The node user is UID 1000, matching the usual WSL2 user.
RUN mkdir -p node_modules .next && chown -R node:node /app
USER node

# Source is bind-mounted in development; only the dependencies live in the image.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

EXPOSE 3000
CMD ["npm", "run", "dev"]

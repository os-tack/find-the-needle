FROM node:20-alpine

RUN apk add --no-cache redis curl bash

WORKDIR /app

COPY app/package.json .
RUN npm install --production

COPY app/ .

COPY test.sh /test.sh
RUN chmod +x /test.sh

EXPOSE 3000

CMD ["node", "server.js"]

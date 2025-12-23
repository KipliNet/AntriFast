FROM node:20-alpine 
WORKDIR /app
RUN apk add --no-cache git openssh
COPY package*.json ./
RUN npm install --omit=dev

COPY src ./src
EXPOSE 3000
CMD ["node", "src/index.js"]

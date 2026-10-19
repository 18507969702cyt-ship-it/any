const http = require('http');

const PORT = process.env.PORT || 3000;

const server = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify({
    status: 'ok',
    message: 'Hello from Alibaba Cloud ECS!',
    server: 'Ubuntu-gmku',
    time: new Date().toISOString(),
    path: req.url
  }));
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running on http://0.0.0.0:${PORT}`);
  console.log(`Time: ${new Date().toISOString()}`);
});

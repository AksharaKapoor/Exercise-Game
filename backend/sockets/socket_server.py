import asyncio
import json
import logging
import websockets
import random
import ssl
import argparse

logging.basicConfig()

logging.getLogger().setLevel(logging.INFO)

'''
need to handle:
- joining the game
    - when they load into the room URL, they'll open a websocket
        - on first connection, we can assign random player name?
        - client sends JOIN_GAME(websocket, room) to server
    - wait for one player to start game
        - one client sends START_GAME(websocket, room) to server
        - make sure there's no funky things going on if multiple people click the button (e.g. GAME_STARTED = True)
- start game
    - send prepare frontend message to every client
        - send SETUP_GAME(ready_time, rounds, players) to clients
            - give X seconds to allow players to get in place and load PoseNet model
    - start a round
        - send START_ROUND(round_num, img_name, round_duration) to clients
        - wait to get score for a round from each client
            - clients send SEND_SCORE(websocket, room, round_num, score) to server
            - sending scores to everyone else for leaderboard
        - server sends UPDATE_LEADERBOARD({player: score, ...}) to clients
        - then start a new round until game ends
- end game
    - send END_GAME() to clients?


- (later) live updating statuses?
'''

# Maps room IDs to Game objects.
ROOMS = {}

# map websockets to games
USERS = {}

# Need to keep in sync with /frontend/public/img/*.
IMAGE_NAMES = ['dance.png', 'eagle.png', 'garland.png', 'gate.png', 'half-moon.png', 'parivrtta-trikonasana.png', 'vrksasana.png', 
'warrior-I.png', 'warrior-II.png', 'bigtoepose.jpg', 'chairpose.jpg']

class Player:
    def __init__(self, websocket, game, name):
        self.websocket = websocket
        self.game = game
        self.round_scores = []
        self.name = name
        self.ready = False
    
    async def send(self, data):
        await self.websocket.send(json.dumps(data))

class Game:
    def __init__(self, room):
        self.room = room
        self.total_rounds = 7 # maybe change later
        self.current_round = 0
        # map websocket to player objects
        self.players = {} 

        self.used_images = set()

    def add_player(self, websocket, name):
        player = Player(websocket, self, name)
        self.players[websocket] = player

        logging.info('added player {} to game in room {}'.format(player.name, self.room))
    
    async def remove_player(self, websocket):
        if websocket not in self.players:
            return
        logging.info('removed player {} from game in room {}'.format(self.players[websocket].name, self.room))
        self.players.pop(websocket)

        if len(self.players) == 0:
            await self.end()
    
    def get_scores(self):
        return {
            player.name: player.round_scores for player in self.players.values()
        }
    
    async def ready_player(self, websocket):
        self.players[websocket].ready = True

        logging.info('player {} ready for room {}'.format(self.players[websocket].name, self.room))

        if sum([p.ready for p in self.players.values()]) == len(self.players):
            await self.start_round()
    
    async def start_round(self):

        logging.info('starting round {} in room {}'.format(self.current_round, self.room))

        image = random.choice(IMAGE_NAMES)
        while image in self.used_images:
            image = random.choice(IMAGE_NAMES)

        duration = random.randint(10, 20), # TODO: tune duration?
        self.used_images.add(image)
        
        await self.notify_players({
            'action': 'START_ROUND',
            'roundDuration': duration,
            'imageName': image,
            'currentRound': self.current_round,
            'totalRounds': self.total_rounds,
            'prevScores': self.get_scores(),
        })
    
    async def send_score(self, websocket, score):
        player = self.players[websocket]
        player.round_scores.append(score)

        logging.info('player {} sending in score to room {}'.format(player.name, self.room))

        # if all scores are in, start next round
        if sum(len(p.round_scores) == self.current_round + 1 for p in self.players.values()) == len(self.players):
            self.current_round += 1
            if self.current_round == self.total_rounds:
                await self.end()
            else:
                await self.start_round()
    
    async def end(self):

        logging.info('game ending in room {}'.format(self.room))

        await self.notify_players({
            'action': 'END_GAME',
            'totalRounds': self.total_rounds,
            'prevScores': self.get_scores(),
        })
        ROOMS.pop(self.room)
        

    async def notify_players(self, data):
        for _, player in self.players.items():
            await player.send(data)

'''
TEST SEQUENCE:
In JS:

ws = new WebSocket('ws://localhost:6789')

ws.onmessage = function (event) {
                data = JSON.parse(event.data);
                console.log('data received');
                console.log(data);
            };

ws.send(JSON.stringify({action: 'JOIN_GAME', room: '1', name: 'bob'}))

ws.send(JSON.stringify({action: 'SET_READY', room: '1'}))

ws.send(JSON.stringify({action: 'FINISH_ROUND', room: '1', score: '5'}))


'''

async def join_or_create_game(websocket, room, name):
    ROOMS.setdefault(room, Game(room))
    game = ROOMS[room]
    USERS[websocket] = game
    game.add_player(websocket, name)

async def handler(websocket, path):
    try:
        async for message in websocket:
            data = json.loads(message)

            if "action" not in data:
                logging.error("no action: {}".format(data))
                continue

            if data["action"] == "JOIN_GAME":
                room = data['room']
                name = data['name']
                await join_or_create_game(websocket, room, name)
            elif data["action"] == "SET_READY":
                room = data['room']
                if room not in ROOMS:
                    logging.error("no game in room: {}".format(data))
                    continue
                game = ROOMS[room]
                await game.ready_player(websocket)
            elif data["action"] == "FINISH_ROUND":
                room = data['room']
                if room not in ROOMS:
                    logging.error("no game in room: {}".format(data))
                    continue
                game = ROOMS[room]
                score = data['score']
                await game.send_score(websocket, score)
            else:
                logging.error("unsupported event: {}".format(data))
    finally:
        if websocket in USERS:
            game = USERS[websocket]
            await game.remove_player(websocket)
            USERS.pop(websocket)



parser = argparse.ArgumentParser(description='host')
parser.add_argument('--host', default='0.0.0.0', type=str)
parser.add_argument('--env', default='dev', type=str)
args = parser.parse_args()

if args.env == "prod":
    # ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # ssl_context.load_verify_locations('/etc/letsencrypt/live/socket.poseparty.brian.lol/privkey.pem')
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certfile='/etc/letsencrypt/live/socket.poseparty.brian.lol/cert.pem', keyfile='/etc/letsencrypt/live/socket.poseparty.brian.lol/privkey.pem')
    start_server = websockets.serve(handler, args.host, 6789, ssl=context)
else:
    start_server = websockets.serve(handler, args.host, 6789)

asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()

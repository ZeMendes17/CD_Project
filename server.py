from worker import processMusic
import base64
from flask import Flask, request, send_file, render_template, jsonify, redirect, url_for

import random
import threading
from io import BytesIO
from mutagen.id3 import ID3
from pydub import AudioSegment
from celery import Celery
from sys import getsizeof
import gc

# to remove the files and directories of the static directory
import os
import shutil

# from memory_profiler import profile


app = Flask(__name__)
celery = Celery()
id_usados = []
musicas = []
idBytes = {}
idTracks = {}
callbacks = {}
jobCallback = {} # dict to store --> key: id, value: job
jobs = []
percentage = 0
tracks = []
idProgress = {}


# remove all the directories and files inside the static folder
# this is done so that the files are not stored in the server
# after the server is closed
# as no database is used, this is the best way to do it
# eleminates the directory and all the files inside it

for root, dirs, files in os.walk('static', topdown=False):
    for file in files:
        # Remove files
        print('Removing file: ', file)
        filePath = os.path.join(root, file)
        os.remove(filePath)
    for dir in dirs:
        # Remove directories
        print('Removing directory: ', dir)
        dirPath = os.path.join(root, dir)
        shutil.rmtree(dirPath)


# define the classes
class Music:
    def __init__(self, id, name, band, tracks):
        self.music_id = id
        self.name = name
        self.band = band
        self.tracks = tracks

    def __repr__(self):
        return f"Music({self.music_id}, {self.name}, {self.band}, {self.tracks})"
    
class Track:
    def __init__(self, id, name):
        self.track_id = id
        self.name = name

    def __repr__(self):
        return f"Track({self.name},{self.track_id})"
    
class Instrument:
    def __init__(self, name, track):
        self.name = name
        self.track = track
        
    def __repr__(self):
        return f"Instrument({self.name}, {self.track})"

class Progress:
    def __init__(self, progress, instruments, final):
        self.progress = progress
        self.instruments = instruments
        self.final = final

    def __repr__(self):
        return f"Progress({self.progress}, {self.instruments}, {self.final})"
        
class Job:
    def __init__(self, id, size, time, music_id, track_id):
        self.job_id = id
        self.size = size
        self.time = time
        self.music_id = music_id
        self.track_id = track_id

    def __repr__(self):
        return f"Job({self.job_id}, {self.size}, {self.time}, {self.music_id}, {self.track_id})"
    

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/music', methods=['POST'])
def music_post():
    try:
        # gets the file from the request
        file = request.files['myfile']
        # gets the file to bytes
        fileBytes = file.read()

        # create a mutex to lock the counter
        mutex = threading.Lock()

        # gets the music info, name and band
        try:
            music = ID3(fileobj=BytesIO(fileBytes))
            name = music['TIT2'].text[0] if music['TIT2'] else 'Unknown'
            band = music['TPE1'].text[0] if music['TPE1'] else 'Unknown'
        except Exception as e:
            print('Error: ', e)
            name = 'Unknown'
            band = 'Unknown'   

        # lock the mutex
        mutex.acquire()

        try:
            # creates the music object instance
            musicObj = createMusicObj(name, band)

            # store the id and the bytes of the music
            idBytes[musicObj.music_id] = fileBytes

        finally:
            # release the mutex
            mutex.release()
        
        # returns the music info
        return jsonify(toDict(musicObj)), 200
    
    except Exception as e:
        print('Error: ', e)
        return 'Invalid input', 405

    

@app.route('/music', methods=['GET'])
def music_get():
    result = []
    # foreach music in the musicas list converts it to a dict to return after
    for music in musicas:
        result.append(toDict(music))
        
    return jsonify(result)

@app.route('/redirectJob', methods=['GET'])
def redirect_job():
    jobID = int (request.args.get('id3'))
    return redirect(url_for('job_get_id', id=jobID))

@app.route('/redirect', methods=['POST', 'GET'])
def redirect_post():

    if request.method == 'POST':
        # gets the music id from the request
        musicID = int (request.form.get('id'))

        bass = (request.form.get('bass'))
        drums = (request.form.get('drums'))
        vocals = (request.form.get('vocals'))
        other = (request.form.get('other'))
        
        if bass == None and drums == None and vocals == None and other == None:
            return 'No tracks were selected for this music. Please select at least one track to separate'
        
        tracks = []
        if bass != None:
            tracks.append('bass')
        if drums != None:
            tracks.append('drums')
        if vocals != None:
            tracks.append('vocals')
        if other != None:
            tracks.append('other')

        # stores the tracks to be separated with the music id
        idTracks[musicID] = tracks

        # redirects to the music_id_post method with the music id inserted
        return redirect(url_for('music_id_post', id=musicID), code=307) # 307 is the code for redirecting to POST method instead of GET
    
    else:
        # gets the music id from the request
        musicID = int (request.args.get('id2'))  
        
        return redirect(url_for('music_id_get', id=musicID))

@app.route('/music/<id>', methods=['POST'])
# @profile
def music_id_post(id):

    if int(id) not in idBytes.keys():
        return 'Music not found', 404
    
    if int(id) in idTracks.keys():
        return 'Music already submitted', 405


    if int(id) not in idTracks.keys():
        instruments = request.form.get('instruments')
        idTracks[int(id)] = instruments.split(',')
        if instruments == None:
            return 'Invalid Track', 405

        for instrument in idTracks[int(id)]:
            if instrument not in ['bass', 'drums', 'vocals', 'other']:
                return 'Invalid Track', 405

    

    taskCounter = 0
    mutex = threading.Lock()

    # id is received as a string, so it is converted to int
    id = int(id)
    # if id does not exist, meaning that the music was not submitted
    if id not in id_usados:
        return 'Music not found', 404

    # the music is going to be processed by the worker here
    musicBytes = idBytes[id]

    # total duration of the song
    audio = AudioSegment.from_file(BytesIO(musicBytes), format='mp3')
    totalDuration = len(audio) / 1000 # in seconds

    # dinamically get the best chunk duration to split the music
    duration = chunkDuration(totalDuration)    

    # splits the music into chunks based on the chunk duration
    chunks = splitMusic(musicBytes, duration)

    # create a mutex to lock the counter
    mutex.acquire()

    try:

        # iterate through the chunks
        for chunk in chunks:
            # process the music with the selected tracks
            callback = processMusic.apply_async(args=(encodeMusic(chunk), taskCounter))
            # store the job in the jobs dict
            jobID = generateID()
            jobCallback[callback] = jobID

            jobSize = getsizeof(chunk)
            musicID = id
            track_id = []

            # create a job object
            job = Job(jobID, jobSize, None, musicID, track_id)

            # store the job in the jobs list
            jobs.append(job)


            if id in callbacks.keys():
                callbacks[id].append(callback)
            else:
                callbacks[id] = [callback]

            taskCounter += 1
        
    finally:
        # release the mutex
        mutex.release()

    return 'successful operation', 200

@app.route('/music/<id>', methods=['GET'])
# @profile
def music_id_get(id):
    ## get the state of the task, if it is at 100% it is done -> generate the file

    if int(id) not in callbacks:
        return 'Music not found', 404

    # no need to see everything again
    if int(id) in idProgress.keys():
        progress = idProgress[int(id)]
        return jsonify(toDictProgress(progress)), 200
        # return render_template('generatedLinks.html', instrumentLinks=progress.instruments, finalLink=progress.final)    
    
    cbs = callbacks[int(id)]
    total = 0
    successes = 0

    for cb in cbs:
        # print(cb.state) # shows the state of each task sent SUCCESS, PENDING, FAILURE
        total += 1
        if(cb.state == 'SUCCESS'):
            jobID = jobCallback[cb]
            timestamp = cb.info[1]

            for job in jobs:
                if job.job_id == jobID and job.time == None and job.track_id == []:
                    job.time = timestamp
                    tracksTemp = []
                    for key in range(4):
                        trackID = generateID()

                        #tracks.append(Track(trackID, key))
                        tracksTemp.append(trackID)

                    job.track_id = tracksTemp
            successes += 1
              
    # print(str(successes) + " -----> " + str(total))
    percentage = int(successes / total * 100)

    progress = Progress(percentage, None, None)

    # if the music is still being processed
    if percentage != 100:
        return jsonify(
            {
                'progress': percentage,
                'instruments': [
                    {
                        "name": 'bass',
                        "track": ''
                    },
                    {
                        "name": 'drums',
                        "track": ''
                    },
                    {
                        "name": 'vocals',
                        "track": ''
                    },
                    {
                        "name": 'other',
                        "track": ''
                    }
                ],
                'final': ''
            }
        ), 200

    # if it is at 100% but info is not yet available (should not happen)
    for cb in cbs:
        if(cb.info == None):
            return jsonify(
            {
                'progress': percentage,
                'instruments': [
                    {
                        "name": 'bass',
                        "track": ''
                    },
                    {
                        "name": 'drums',
                        "track": ''
                    },
                    {
                        "name": 'vocals',
                        "track": ''
                    },
                    {
                        "name": 'other',
                        "track": ''
                    }
                ],
                'final': ''
            }
        ), 200

    # get the instruments selected by the user
    instruments = idTracks[int(id)]

    # create the music directory to store the .wav files
    if not os.path.exists('static/' + str(id)):
        os.makedirs('static/' + str(id))


    # join the files from each task into one file
    # for example, bass0.wav, bass1.wav, bass2.wav --> bass.wav
    bass = []
    drums = []
    vocals = []
    other = []

    allInstruments = {}

    for cb in cbs:
        for key in cb.info[0].keys():
            if 'bass' in key:
                bass.append(key)
            elif 'drums' in key:
                drums.append(key)
            elif 'vocals' in key:
                vocals.append(key)
            elif 'other' in key:
                other.append(key)

            # store all the instruments in a list to access them easily
            allInstruments[key] = base64.b64decode(cb.info[0][key])
            

    # now with them in the lists, we can join them in order 0, ... ,n
    bass = sorted(bass, key=lambda x: int(x[4:]))
    drums = sorted(drums, key=lambda x: int(x[5:]))
    vocals = sorted(vocals, key=lambda x: int(x[6:]))
    other = sorted(other, key=lambda x: int(x[5:]))


    # finnaly, we can join them using AudioSegment


    # bass
    final = AudioSegment.from_file(BytesIO(allInstruments[bass[0]]), format="wav")
    for i in range(1, len(bass)):
        final += AudioSegment.from_file(BytesIO(allInstruments[bass[i]]), format="wav")
    final.export('static/' + str(id) + '/bass.wav', format='wav')
    
    # drums
    final = AudioSegment.from_file(BytesIO(allInstruments[drums[0]]), format="wav")
    for i in range(1, len(drums)):
        final += AudioSegment.from_file(BytesIO(allInstruments[drums[i]]), format="wav")
    final.export('static/' + str(id) + '/drums.wav', format='wav')

    # vocals
    final = AudioSegment.from_file(BytesIO(allInstruments[vocals[0]]), format="wav")
    for i in range(1, len(vocals)):
        final += AudioSegment.from_file(BytesIO(allInstruments[vocals[i]]), format="wav")
    final.export('static/' + str(id) + '/vocals.wav', format='wav')

    # other
    final = AudioSegment.from_file(BytesIO(allInstruments[other[0]]), format="wav")
    for i in range(1, len(other)):
        final += AudioSegment.from_file(BytesIO(allInstruments[other[i]]), format="wav")
    final.export('static/' + str(id) + '/other.wav', format='wav')


    # overlay the files in files to create the final .wav file
    # using the pydub library with the AudioSegment class
    files = []

    for instrument in instruments:
        files.append('static/' + str(id) + '/' + instrument + '.wav')

    returnFile = AudioSegment.from_file(files[0])

    for i in range(1, len(files)):
        returnFile = returnFile.overlay(AudioSegment.from_file(files[i]))
    returnFile.export('static/' + str(id) + '/final.wav', format='wav')

    # the return will render the html page with links to the files
    instruments = []

    for instrument in ['bass', 'drums', 'vocals', 'other']:
        instruments.append(Instrument(instrument, 'localhost:5000/static/' + str(id) + '/' + instrument + '.wav'))

    final = 'localhost:5000/static/' + str(id) + '/final.wav'

    progress.final = final
    progress.instruments = instruments

    idProgress[int(id)] = progress

    return jsonify(toDictProgress(progress)), 200
    # return render_template('generatedLinks.html', instrumentLinks=progress.instruments, finalLink=progress.final)

@app.route('/job', methods=['GET'])
def job_get():
    try:
        returnJobs = []

        for job in jobs:
            returnJobs.append(job.job_id)

        return jsonify(returnJobs)
    except:
        return 'Invalid input', 405

@app.route('/job/<id>', methods=['GET'])
def job_get_id(id):
    # returns id, size, time, music_id, track_id
    for job in jobs:
        if int(id) == job.job_id:
            return jsonify(toDictJob(job)), 200                                                                                                
    
    return 'Job not found', 405


@app.route('/reset', methods=['POST'])
def reset():

    global id_usados
    global musicas
    global idBytes
    global jobs
    global idTracks
    global percentage
    global callbacks
    global tracks
    global idProgress
    global jobCallback

    # reset the workers
    command = 'celery -A worker purge -f'
    os.system(command)

    for cbs in callbacks.values():
        for cb in cbs:
            print('Revoking task: ', cb.id)
            celery.control.revoke(cb.id, terminate=True)

    # clear all the global variables
    id_usados = []
    musicas = []
    idBytes = {}
    idTracks = {}
    callbacks = {}
    jobCallback = {} # dict to store --> key: id, value: job
    jobs = []
    percentage = 0
    tracks = []
    idProgress = {}

    # delete all the files in the static folder
    for root, dirs, files in os.walk('static', topdown=False):
        for file in files:
            # Remove files
            print('Removing file: ', file)
            filePath = os.path.join(root, file)
            os.remove(filePath)
        for dir in dirs:
            # Remove directories
            print('Removing directory: ', dir)
            dirPath = os.path.join(root, dir)
            shutil.rmtree(dirPath)

    gc.collect()

    return 'successful operation', 200


# creates the music object
def createMusicObj(name, band):
    tracksData = [ 
            { 
                "name": "drums",
                "track_id": generateID()
            },
            {
                "name": "bass",
                "track_id": generateID()
            },
            {
                "name": "vocals",
                "track_id": generateID()
            },
            {
                "name": "other",
                "track_id": generateID()
            }
            ]
    

    # create a list of Track objects
    tracksTemp = [Track(tracksData["track_id"], tracksData["name"]) for tracksData in tracksData]

    # store the tracks in a dict
    for track in tracksTemp:
        tracks.append(track)

    # if the music already exists in the server, there is no need to create a new Obj   
    # fro now if it is known --> new music
    # for musica in musicas:
    #     if musica.name == name and musica.name != 'Unknown' and musica.band == band and musica.band != 'Unknown':
    #         return musica
    

    id = generateID()


    music = Music(id, name, band, tracksTemp)


    # stores the music info into a dict
    musicas.append(music)
    
    return music

# function to convert the music object to a dict
def toDict(music: Music):

    tracks = []

    for track in music.tracks:
        tracks.append({
            "name": track.name,
            "track_id": track.track_id
        })

    return {
        "music_id": music.music_id,
        "name": music.name,
        "band": music.band,
        "tracks": tracks
    }

# function to convert the job object to a dict
def toDictJob(job : Job):
    return {
        "job_id": job.job_id,
        "size": job.size,
        "time": job.time,
        "music_id": job.music_id,
        "track_id" : job.track_id
    }

def toDictProgress(progress : Progress):
    temp = []
    for instrument in progress.instruments:
        temp.append({
            "name": instrument.name,
            "track": instrument.track
        })

    return {
        "progress": progress.progress,
        "instruments": temp,
        "final": progress.final
    }


# generates and stores the id to a id list in order to not repeat ids
def generateID():
    id = random.randint(100000, 999999)
    while id in id_usados:
         id = random.randint(100000, 999999)
    
    id_usados.append(id)
    return id
    
# function to encode the music bytes to base64
def encodeMusic(musicBytes):
    encoded = base64.b64encode(musicBytes).decode('utf-8')
    return encoded

def chunkDuration(total_duration):
    # dependde dos numero de workers
    # Set an initial target duration
    if total_duration <= 10:
        target_duration = total_duration
    
    elif total_duration <= 60:
        target_duration = 10

    elif total_duration <= 60 * 5:
        target_duration = 30

    elif total_duration <= 60 * 10:
        target_duration = 60

    else :
        target_duration = 120

    return target_duration



# function to split the audio file into segments
def splitMusic(musicBytes, chunkDuration):
    # Create an audio segment from the input music bytes
    audio = AudioSegment.from_file(BytesIO(musicBytes), format='mp3')

    # Calculate the chunk length in milliseconds
    length = int(chunkDuration * 1000)

    chunks = []
    totalDuration = len(audio) 

    for start in range(0, totalDuration, length):
        end = min(start + length, totalDuration)
        chunk = audio[start:end]
        chunks.append(chunk.export(format='mp3').read())

    return chunks
    

if __name__ == '__main__':
    app.run()
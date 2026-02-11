# Option 1: Clone und direkt starten (von überall)
git clone https://github.com/A9xxx/Install-E3DC-Control.git
cd Install-E3DC-Control
sudo python3 Install/installer_main.py

# Option 2: Clone zu ~/Install (von deinem neuen Pi)
cd ~
git clone https://github.com/A9xxx/Install-E3DC-Control.git Install-E3DC-Control-neu
cd Install-E3DC-Control-neu
sudo python3 Install/installer_main.py

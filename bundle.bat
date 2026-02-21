pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name Filigrane `
  --icon=img\filig.ico `
  --add-data "img;img" `
  --collect-all PIL `
  --collect-all rawpy `
  --hidden-import PIL._tkinter_finder `
  .\filigrane.pyw
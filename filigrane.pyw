import os
import traceback
from tkinter import *
from tkinter import filedialog
from tkinter import ttk
from tkinter.messagebox import showinfo, showerror
from PIL import Image, ImageOps


EXTS = ('.jpg', '.png')
def resizeWatermark(prevLogo, wantedWidth):
    wantedHeight = (wantedWidth/prevLogo.width)*prevLogo.height
    return prevLogo.resize((int(wantedWidth), int(wantedHeight)))

def disableInteractions():
    button_explore_in["state"] = "disabled"
    button_explore_out["state"] = "disabled"
    button_explore_wm["state"] = "disabled"
    button_start["state"] = "disabled"
    opacityScale["state"] = "disabled"
    Tk.update(window)

def enableInteractions():
    button_explore_in["state"] = "normal"
    button_explore_out["state"] = "normal"
    button_explore_wm["state"] = "normal"
    button_start["state"] = "normal"
    opacityScale["state"] = "normal"
    Tk.update(window)

def updateProgression(filename="", nb=0, started=True):
    if (started):
        txtCurrentOperation.set("Watermarking : \""+ filename + "\"")
    txtcounter.set(""+ str(nb) + "/" + str(nbImageToEdit.get()))
    txtButtonStart.set("Watermark " + str(nbImageToEdit.get()) + " images")
    if(nbImageToEdit.get()>0):
        progressbar_operation['value'] = nb*100/nbImageToEdit.get()
    Tk.update(window)

def finishOperations(success = True):
    enableInteractions()
    progressbar_operation['value']=0
    txtCurrentOperation.set("")
    if success :
        showinfo("Filigrane progress finished", "Watermarks added, you can see results in " + folderOutPath.get())

def filigrane(pathVar, pathOutVar, lgoVar):
    try:
        disableInteractions()
        transparency = opacity.get()
        path = pathVar.get()
        pathOut = pathOutVar.get()
        os.makedirs(pathOut, exist_ok=True)
        lgo = lgoVar.get()
        logo = Image.open(lgo).convert('RGBA')
        pixeldata = list(logo.getdata())
        for i,pixel in enumerate(pixeldata):
            r = pixel[0]
            g = pixel[1]
            b = pixel[2]
            a = pixel[3]
            alpha = int(256*transparency)
            alpha = (a - (256 - alpha)) if (a - (256 - alpha)) > 0 else 0 
            if pixel[3] != 0:
                pixeldata[i] = (r, g, b, alpha)

        logo.putdata(pixeldata)
        
        # Appareil photo de vanessa : 6000*4000
        vanessaWidth = 6000
        vanessaHeight = 4000

        landscapeLogo = resizeWatermark(logo, vanessaWidth/2)
        portraitLogo = resizeWatermark(logo, vanessaHeight)
        nbImageToEdit.set(0)
        for filename in os.listdir(path):
            if any([filename.lower().endswith(ext) for ext in EXTS]) and filename != lgo:
                nbImageToEdit.set(nbImageToEdit.get()+1)
        nb = 0
        for filename in os.listdir(path):
            if any([filename.lower().endswith(ext) for ext in EXTS]) and filename != lgo:
                
                nb=nb+1
                updateProgression(filename=filename, nb=nb)
                image = Image.open(path + '/' + filename)
                #Use exif data to have the image with the right orientation
                image = ImageOps.exif_transpose(image)
                imageWidth = image.width
                imageHeight = image.height
                if imageWidth==vanessaWidth:
                    #Landscape image
                    image.paste(landscapeLogo, (int((imageWidth - landscapeLogo.width)/2), int((imageHeight - landscapeLogo.height)/2)), landscapeLogo)
                elif imageWidth == vanessaHeight:
                    #Portrait image
                    image.paste(portraitLogo, (int((imageWidth - portraitLogo.width)/2), int((imageHeight - portraitLogo.height)/2)), portraitLogo)
                else :
                    #When the size of the photo is unknown, we resize the logo
                    specialLogo = resizeWatermark(logo, imageWidth/2)
                    image.paste(specialLogo, (int((imageWidth - specialLogo.width)/2), int((imageHeight - specialLogo.height)/2)), specialLogo)
                
                image.save(pathOut + '/wm_' + filename)
                print('Added watermark to ' + path + '/' + filename + ' in ' + pathOut + '/wm_' + filename)
        finishOperations()
    except Exception as e:
        print(traceback.format_exc())
        showerror(title="Erreur", message="An error has occured, please check your paths \n\n Trace : \n" + traceback.format_exc())
        finishOperations(False)


    

def updateOpacity(var):
    opacity.set(round(float(var)/float(100.0), 2))

def browseFolder(var):
    if var==1:
        newpath = filedialog.askdirectory()
        if(bool(newpath)) :
            folderInPath.set(newpath)
            for filename in os.listdir(newpath):
                if any([filename.lower().endswith(ext) for ext in EXTS]):
                    nbImageToEdit.set(nbImageToEdit.get()+1)
            updateProgression(started=False)

    elif var==2:
        newpath = filedialog.askdirectory()
        folderOutPath.set(newpath if bool(newpath) else folderOutPath.get())
    elif var==3:
        newpath = filedialog.askopenfilename(initialdir = "/",
                                        title = "Select a File",
                                        filetypes = (("Pngs",
                                                    "*.png*"),
                                                    ("Jpgs",
                                                    "*.jpg*")))
        waterkmarkPath.set(newpath if bool(newpath) else waterkmarkPath.get())
    if (os.path.isdir(folderInPath.get())) and os.path.isdir(folderOutPath.get()) and os.path.isfile(waterkmarkPath.get()) :
        button_start["state"]="normal"
    else :
        button_start["state"]="disabled"
    
          

window = Tk()
window.title('Filigrane')
window.config(background="white")
mainframe = ttk.Frame(window, padding="3 3 12 12")
mainframe.grid(column=0, row=0, sticky=(N, W, E, S))
window.columnconfigure(0, weight=1)
window.rowconfigure(0, weight=1)

nbImageToEdit = IntVar(0)

#ROW 1
    #COL 2
photo = PhotoImage(file = "./img/default_watermark.png")
photoImage = photo.subsample(4,4)
lblimg = ttk.Label(mainframe, image=photoImage)
lblimg.grid(column=2, row=1, sticky=(W,E))

#ROW 2
folderInPath = StringVar()
#folderInPath.set("./images/")
folderInPath.set("Select a folder")
    #COL 1
label_explore_in = ttk.Label(mainframe, text="Source images path")
label_explore_in.grid(column=1, row=2, sticky=(E))
    #COL 2
entry_explore_in = ttk.Entry(mainframe, textvariable=folderInPath, state="disabled")
entry_explore_in.grid(column=2, row=2, sticky=(W, E))
    #COL 3
button_explore_in = Button(mainframe, text="Browse folders", command=lambda : browseFolder(1))
button_explore_in.grid(column=3, row=2, sticky=(W, E))

#ROW 3
folderOutPath = StringVar()
#folderOutPath.set("./images/watermarked/")
folderOutPath.set("Select a folder")
    #COL 1
label_explore_out = ttk.Label(mainframe, text="Watermarked images path")
label_explore_out.grid(column=1, row=3, sticky=(E))
    #COL 2
entry_explore_out = ttk.Entry(mainframe, textvariable=folderOutPath, state="disabled")
entry_explore_out.grid(column=2, row=3, sticky=(W, E))
    #COL 3
button_explore_out = Button(mainframe, text="Browse folders", command=lambda : browseFolder(2))
button_explore_out.grid(column=3, row=3, sticky=(W, E))

#ROW 4
waterkmarkPath = StringVar()
waterkmarkPath.set("./img/default_watermark.png")
    #COL 1
label_explore_wm = ttk.Label(mainframe, text="Watermark path")
label_explore_wm.grid(column=1, row=4, sticky=(E))
    #COL 2
entry_explore_wm = ttk.Entry(mainframe, textvariable=waterkmarkPath, state="disabled")
entry_explore_wm.grid(column=2, row=4, sticky=(W, E))
    #COL 3
button_explore_wm = Button(mainframe, text="Browse files", command=lambda : browseFolder(3))
button_explore_wm.grid(column=3, row=4, sticky=(W, E))

#ROW 5
opacity = DoubleVar()
    #COL 1
labelOpacity = ttk.Label(mainframe, text="Opacity")
labelOpacity.grid(column=1, row=5, sticky=(E))
    #COL 2
opacityScale = ttk.Scale(mainframe, from_=0, to=100, command=updateOpacity)
opacityScale.set(100)
opacityScale.grid(column=2, row=5, sticky=(W, E))
    #COL 3
resultOpacity = ttk.Label(mainframe, textvariable=opacity)
resultOpacity.grid(column=3, row=5, sticky=(W, E))

#ROW 6
txtButtonStart = StringVar()
    #COL 2
button_start = Button(mainframe, textvariable=txtButtonStart, command = lambda : filigrane(folderInPath,folderOutPath, waterkmarkPath), state="disabled")
button_start.grid(column=2,row=6, sticky=(E, W))

#ROW 7
txtCurrentOperation = StringVar()
txtcounter = StringVar()
    #COL 1
labelCounter = ttk.Label(mainframe, textvariable=txtcounter)
labelCounter.grid(column=1, row=7)
txtCurrentOperation.set("")
label_current = ttk.Label(mainframe, textvariable=txtCurrentOperation)
label_current.grid(column=2, columnspan=3,row=7, sticky=(E, W))

#ROW 7
progressbar_operation = ttk.Progressbar(mainframe, orient="horizontal", mode="determinate")
progressbar_operation.grid(columnspan=4,row=8, sticky=(E, W))

updateProgression(started=False)

for child in mainframe.winfo_children(): 
    child.grid_configure(padx=5, pady=5)



window.mainloop()
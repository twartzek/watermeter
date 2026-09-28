from ultralytics.engine.results import Results
from ultralytics.engine.results import Boxes
import torch
import cv2
import numpy as np

class ResultsExtended(Results):

    def __init__(self, results):
       
        super().__init__(path=results.path, orig_img=results.orig_img, names=results.names, keypoints=results.keypoints, speed=results.speed)
        self.boxes = Boxes(results.boxes.data, results.boxes.orig_shape)

    def sort_boxes(self,mode="l2r"):
        """
        Sortiert die Bounding Boxes (und zugehörigen Klassen und Wahrscheinlichkeiten)
        in der Ergebnisliste von links nach rechts.
        """
        if self.boxes is not None and len(self.boxes):
            # Kombiniere Boxen, Klassen und Wahrscheinlichkeiten für die Sortierung
            combined_data = []
            for i in range(len(self.boxes)):
                box = self.boxes[i].xyxy.tolist()[0]
                conf = self.boxes[i].conf.item() if self.boxes[i].conf is not None else None
                cls = int(self.boxes[i].cls.item()) if self.boxes[i].cls is not None else None
                combined_data.append((box, conf, cls))

            # Sortiere basierend auf der x_min Koordinate der Bounding Box
            if mode=="l2r":
                sorted_data = sorted(combined_data, key=lambda item: item[0][0])
            else:
                sorted_data = sorted(combined_data, key=lambda item: item[0][0],reverse=True)
            # Entpacke die sortierten Daten zurück in die Ergebnisattribute
            sortedBoxes = torch.tensor( [[item[0][0], item[0][1], item[0][2], item[0][3], item[1], item[2]] for item in sorted_data])

            # Aktualisiere die entsprechenden Attribute des Results-Objekts
            self.boxes = Boxes(sortedBoxes, self.orig_shape)

    def get_bboxes_of_class(self, class_id):
        bboxes = []
        for box in self.boxes:
            if box.cls == class_id:
                bboxes.append(box)
        return bboxes
    

    def filterbboxes(self, refBox, tolerance=10):
        """
        Filtert die Bounding Boxes, die innerhalb der angegebenen Referenz-Box und Toleranz liegen.
        
        Args:
            refBox (list): Koordinaten der Referenz-Box in der Form [x, y, x, y]
            tolerance (int): Toleranz, innerhalb der ein Bounding Box als innerhalb der Referenz-Box betrachtet wird.
                Standardwert: 10
        """
        filteredbboxes = []
        for i in range(len(self.boxes)):
            box = self.boxes[i].xyxy.tolist()[0]
            conf = self.boxes[i].conf.item() if self.boxes[i].conf is not None else None
            cls = int(self.boxes[i].cls.item()) if self.boxes[i].cls is not None else None

            if (box[0] >= refBox[0] - tolerance and
                box[1] >= refBox[1] - tolerance and
                box[2] <= refBox[2] + tolerance and
                box[3] <= refBox[3] + tolerance):
                filteredbboxes.append((box, conf, cls))

        finalBoxes = torch.tensor( [[item[0][0], item[0][1], item[0][2], item[0][3], item[1], item[2]] for item in filteredbboxes])
        self.boxes = Boxes(finalBoxes, self.orig_shape)



def plot_bboxes(resultNeedles, resultDigits):
    fontscale = 0.5
    img = resultNeedles.orig_img # original image
    results = [resultNeedles, resultDigits]

    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), 
          (0, 255, 255), (128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0)]
    for result in results:
        names = result.names # class names dict
        scores = result.boxes.conf.numpy() # probabilities
        classes = result.boxes.cls.numpy() # predicted classes
        boxes = result.boxes.xyxy.numpy().astype(np.int32) # bboxes
        for score, cls, bbox in zip(scores, classes, boxes): # loop over all bboxes
            class_label = names[cls] # class name
            label = f"{class_label} : {score:0.2f}" # bbox label
            # label = f"{class_label}"
            lbl_margin = 3 #label margin
            color = colors[int(cls % len(colors))]  # select color based on class
            img = cv2.rectangle(img, (bbox[0], bbox[1]),
                                (bbox[2], bbox[3]),
                                color=color,
                                thickness=1)
            label_size = cv2.getTextSize(label, # labelsize in pixels 
                                        fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                                        fontScale=fontscale, thickness=1)
            lbl_w, lbl_h = label_size[0] # label w and h
            lbl_w += 2* lbl_margin # add margins on both sides
            lbl_h += 2*lbl_margin
            img = cv2.rectangle(img, (bbox[0], bbox[1]), # plot label background
                                (bbox[0]+lbl_w, bbox[1]-lbl_h),
                                color=color,
                                thickness=-1) # thickness=-1 means filled rectangle
            # choose black or white text depending on background brightness for sufficient contrast
            brightness = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
            text_color = (0, 0, 0) if brightness > 150 else (255, 255, 255)
            cv2.putText(img, label, (bbox[0]+ lbl_margin, bbox[1]-lbl_margin), # write label to the image
                        fontFace=cv2.FONT_HERSHEY_SIMPLEX,
                        fontScale=fontscale, color=text_color,
                        thickness=1)
    return img